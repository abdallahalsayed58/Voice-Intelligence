import math
import os
from dataclasses import dataclass, field, replace
from itertools import chain
from typing import Dict, List, Tuple, Union

import numpy as np
import torch
import torch.distributed as dist
import random
import torchaudio
import bitsandbytes as bnb
from coqpit import Coqpit
from librosa.filters import mel as librosa_mel_fn
from torch import nn
from torch.cuda.amp.autocast_mode import autocast
from torch.nn import functional as F
from torch.utils.data import DataLoader
from torch.utils.data.sampler import WeightedRandomSampler
from trainer.torch import DistributedSampler, DistributedSamplerWrapper
from trainer.trainer_utils import get_optimizer, get_scheduler

from TTS.tts.configs.shared_configs import CharactersConfig
from TTS.tts.datasets.dataset import TTSDataset, _parse_sample, F0Dataset
from TTS.tts.layers.generic.aligner import AlignmentNetwork
from TTS.tts.layers.naturalspeech2.diffusion import Diffusion
from TTS.tts.layers.naturalspeech2.hificodec.vqvae import VQVAE
from TTS.utils.audio.numpy_transforms import f0_to_coarse
# from TTS.tts.layers.naturalspeech2.descript_audio_codec.dac.utils import process as encode
# from TTS.tts.layers.naturalspeech2.descript_audio_codec.dac.utils import process as decode
# from TTS.tts.layers.naturalspeech2.descript_audio_codec.dac.utils import load_model
# from TTS.tts.layers.naturalspeech2.descript_audio_codec.dac.model import DAC
from TTS.tts.layers.naturalspeech2.encoder import TransformerEncoder
from TTS.tts.layers.naturalspeech2.predictor import ConvBlockWithPrompting
from TTS.tts.layers.glow_tts.duration_predictor import DurationPredictor
from TTS.tts.utils.speakers import SpeakerManager
from TTS.tts.models.base_tts import BaseTTS
from TTS.tts.utils.helpers import (
    average_over_durations,
    generate_path,
    maximum_path,
    rand_segments,
    segment,
    sequence_mask,
)
from TTS.tts.utils.data import prepare_data
from TTS.tts.utils.languages import LanguageManager
from TTS.tts.utils.synthesis import synthesis
from TTS.tts.utils.text.characters import BaseCharacters, _characters, _pad, _phonemes, _punctuations
from TTS.tts.utils.text.tokenizer import TTSTokenizer
from TTS.tts.utils.visual import plot_alignment, plot_spectrogram, plot_avg_pitch
from TTS.utils.io import load_fsspec
from TTS.utils.samplers import BucketBatchSampler
from TTS.vocoder.utils.generic_utils import plot_results
# from vocos import Vocos

##############################
# IO / Feature extraction
##############################

# pylint: disable=global-statement
hann_window = {}
mel_basis = {}




def dynamic_range_compression(x, C=1, clip_val=1e-5):
    """
    PARAMS
    ------
    C: compression factor
    """
    return torch.log(torch.clamp(x, min=clip_val) * C)


def dynamic_range_decompression(x, C=1):
    """
    PARAMS
    ------
    C: compression factor used to compress
    """
    return torch.exp(x) / C

def _amp_to_db(x, C=1, clip_val=1e-5):
    return torch.log(torch.clamp(x, min=clip_val) * C)


def _db_to_amp(x, C=1):
    return torch.exp(x) / C


def amp_to_db(magnitudes):
    output = _amp_to_db(magnitudes)
    return output


def db_to_amp(magnitudes):
    output = _db_to_amp(magnitudes)
    return output


def wav_to_spec(y, n_fft, hop_length, win_length, center=False):
    """
    Args Shapes:
        - y : :math:`[B, 1, T]`

    Return Shapes:
        - spec : :math:`[B,C,T]`
    """
    y = y.squeeze(1)

    global hann_window
    dtype_device = str(y.dtype) + "_" + str(y.device)
    wnsize_dtype_device = str(win_length) + "_" + dtype_device
    if wnsize_dtype_device not in hann_window:
        hann_window[wnsize_dtype_device] = torch.hann_window(win_length).to(dtype=y.dtype, device=y.device)

    padding_ = min(int((n_fft - hop_length) / 2), y.shape[-1] - 1)
    y = torch.nn.functional.pad(
        y.unsqueeze(1),
        (padding_, padding_),
        mode="reflect",
    )
    y = y.squeeze(1)

    spec = torch.stft(
        y,
        n_fft,
        hop_length=hop_length,
        win_length=win_length,
        window=hann_window[wnsize_dtype_device],
        center=center,
        pad_mode="reflect",
        normalized=False,
        onesided=True,
        return_complex=False,
    )

    spec = torch.sqrt(spec.pow(2).sum(-1) + 1e-6)
    return spec


def spec_to_mel(spec, n_fft, num_mels, sample_rate, fmin, fmax):
    """
    Args Shapes:
        - spec : :math:`[B,C,T]`

    Return Shapes:
        - mel : :math:`[B,C,T]`
    """
    global mel_basis
    dtype_device = str(spec.dtype) + "_" + str(spec.device)
    fmax_dtype_device = str(fmax) + "_" + dtype_device
    if fmax_dtype_device not in mel_basis:
        mel = librosa_mel_fn(sr=sample_rate, n_fft=n_fft, n_mels=num_mels, fmin=fmin, fmax=fmax)
        mel_basis[fmax_dtype_device] = torch.from_numpy(mel).to(dtype=spec.dtype, device=spec.device)
    mel = torch.matmul(mel_basis[fmax_dtype_device], spec)
    mel = amp_to_db(mel)
    return mel


def wav_to_mel(y, n_fft, num_mels, sample_rate, hop_length, win_length, fmin, fmax, center=False):
    """
    Args Shapes:
        - y : :math:`[B, 1, T]`

    Return Shapes:
        - spec : :math:`[B,C,T]`
    """
    y = y.squeeze(1)

    global mel_basis, hann_window
    dtype_device = str(y.dtype) + "_" + str(y.device)
    fmax_dtype_device = str(fmax) + "_" + dtype_device
    wnsize_dtype_device = str(win_length) + "_" + dtype_device
    if fmax_dtype_device not in mel_basis:
        mel = librosa_mel_fn(sr=sample_rate, n_fft=n_fft, n_mels=num_mels, fmin=fmin, fmax=fmax)
        mel_basis[fmax_dtype_device] = torch.from_numpy(mel).to(dtype=y.dtype, device=y.device)
    if wnsize_dtype_device not in hann_window:
        hann_window[wnsize_dtype_device] = torch.hann_window(win_length).to(dtype=y.dtype, device=y.device)

    y = torch.nn.functional.pad(
        y.unsqueeze(1),
        (int((n_fft - hop_length) / 2), int((n_fft - hop_length) / 2)),
        mode="reflect",
    )
    y = y.squeeze(1)

    spec = torch.stft(
        y,
        n_fft,
        hop_length=hop_length,
        win_length=win_length,
        window=hann_window[wnsize_dtype_device],
        center=center,
        pad_mode="reflect",
        normalized=False,
        onesided=True,
        return_complex=False,
    )

    spec = torch.sqrt(spec.pow(2).sum(-1) + 1e-6)
    spec = torch.matmul(mel_basis[fmax_dtype_device], spec)
    spec = amp_to_db(spec)
    return spec


#############################
# CONFIGS
#############################


@dataclass
class Naturalspeech2AudioConfig(Coqpit):
    fft_size: int = 1024
    sample_rate: int = 22050
    win_length: int = 1024
    hop_length: int = 320
    num_mels: int = 80
    mel_fmin: int = 0
    mel_fmax: int = None
    pitch_fmax: int = 1100.0
    pitch_fmin: int = 50.0
    do_trim_silence: bool =True
    trim_db: float = 45.0


##############################
# DATASET
##############################


def get_attribute_balancer_weights(items: list, attr_name: str, multi_dict: dict = None):
    """Create inverse frequency weights for balancing the dataset.
    Use `multi_dict` to scale relative weights."""
    attr_names_samples = np.array([item[attr_name] for item in items])
    unique_attr_names = np.unique(attr_names_samples).tolist()
    attr_idx = [unique_attr_names.index(l) for l in attr_names_samples]
    attr_count = np.array([len(np.where(attr_names_samples == l)[0]) for l in unique_attr_names])
    weight_attr = 1.0 / attr_count
    dataset_samples_weight = np.array([weight_attr[l] for l in attr_idx])
    dataset_samples_weight = dataset_samples_weight / np.linalg.norm(dataset_samples_weight)
    if multi_dict is not None:
        # check if all keys are in the multi_dict
        for k in multi_dict:
            assert k in unique_attr_names, f"{k} not in {unique_attr_names}"
        # scale weights
        multiplier_samples = np.array([multi_dict.get(item[attr_name], 1.0) for item in items])
        dataset_samples_weight *= multiplier_samples
    return (
        torch.from_numpy(dataset_samples_weight).float(),
        unique_attr_names,
        np.unique(dataset_samples_weight).tolist(),
    )
# def normalize_pitch(pitch):
#     pitch = np.where(pitch == 0, 1e-10, pitch)  # Replace zeros with a small non-zero value
#     normalized_pitch = np.log(pitch)
#     return normalized_pitch

# def compute_f0(x: np.ndarray, pitch_fmax: int = None, hop_length: int = None, sample_rate: int = None) -> np.ndarray:
#     """Compute pitch (f0) of a waveform using the same parameters used for computing melspectrogram.

#     Args:
#         x (np.ndarray): Waveform.

#     Returns:
#         np.ndarray: Pitch.

#     Examples:
#         >>> WAV_FILE = filename = librosa.util.example_audio_file()
#         >>> from TTS.config import BaseAudioConfig
#         >>> from TTS.utils.audio import AudioProcessor
#         >>> conf = BaseAudioConfig(pitch_fmax=8000)
#         >>> ap = AudioProcessor(**conf)
#         >>> wav = ap.load_wav(WAV_FILE, sr=22050)[:5 * 22050]
#         >>> pitch = ap.compute_f0(wav)
#     """
#     # assert self.pitch_fmax is not None, " [!] Set `pitch_fmax` before caling `compute_f0`."
#     # align F0 length to the spectrogram length
#     if len(x) % hop_length == 0:
#         x = np.pad(x, (0, hop_length // 2), mode="reflect")

#     f0, t = pw.harvest(
#         x.astype(np.double),
#         fs=sample_rate,
#         f0_ceil=pitch_fmax,
#         frame_period=1000 * hop_length / sample_rate,
#     )
#     f0 = pw.stonemask(x.astype(np.double), f0, t, sample_rate)
#     f0 = normalize_pitch(f0)
#     return f0

class Naturalspeech2Dataset(TTSDataset):
    def __init__(self, model_args, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pad_id = self.tokenizer.characters.pad_id
        self.model_args = model_args
        self.f0_dataset = F0Dataset(
            samples=self.samples, ap=self.ap, cache_path=self.f0_cache_path, precompute_num_workers=24
        )
    def __getitem__(self, idx):
        item = self.samples[idx]
        raw_text = item["text"]

        wav = self.ap.load_wav(item["audio_file"],sr=self.ap.sample_rate)
        
        wav_filename = os.path.basename(item["audio_file"])

        token_ids = self.get_token_ids(idx, item["text"]) 
        # get f0 values
        pitch = self.get_f0(idx)["f0"]
        wav = torch.FloatTensor(wav[None,:])
        # after phonemization the text length may change
        # this is a shameful 🤭 hack to prevent longer phonemes
        # TODO: find a better fix
        if len(token_ids) > self.max_text_len or wav.shape[1] < self.min_audio_len:
            self.rescue_item_idx += 1
            return self.__getitem__(self.rescue_item_idx)

        return {
            "raw_text": raw_text,
            "token_ids": token_ids,
            "token_len": len(token_ids),
            "wav": wav,
            "wav_file": wav_filename,
            "speaker_name": item["speaker_name"],
            "language_name": item["language"],
            "audio_unique_name": item["audio_unique_name"],
            "pitch": pitch       
        }

    @property
    def lengths(self):
        lens = []
        for item in self.samples:
            _, wav_file, *_ = _parse_sample(item)
            audio_len = os.path.getsize(wav_file) / 16 * 8  # assuming 16bit audio
            lens.append(audio_len)
        return lens

    def collate_fn(self, batch):
        """
        Return Shapes:
            - tokens: :math:`[B, T]`
            - token_lens :math:`[B]`
            - token_rel_lens :math:`[B]`
            - waveform: :math:`[B, 1, T]`
            - waveform_lens: :math:`[B]`
            - waveform_rel_lens: :math:`[B]`
            - language_names: :math:`[B]`
            - audiofile_paths: :math:`[B]`
            - raw_texts: :math:`[B]`
            - audio_unique_names: :math:`[B]`
        """
        # convert list of dicts to dict of lists
        B = len(batch)
        batch = {k: [dic[k] for dic in batch] for k in batch[0]}

        _, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([x.size(1) for x in batch["wav"]]), dim=0, descending=True
        )

        max_text_len = max([len(x) for x in batch["token_ids"]])
        token_lens = torch.LongTensor(batch["token_len"])
        token_rel_lens = token_lens / token_lens.max()

        wav_lens = [w.shape[1] for w in batch["wav"]]
        wav_lens = torch.LongTensor(wav_lens)
        wav_lens_max = torch.max(wav_lens)
        wav_rel_lens = wav_lens / wav_lens_max

        # format F0
        pitch = prepare_data(batch["pitch"])
        pitch = torch.LongTensor(pitch)[:, None, :].contiguous() # B x 1 xT

        token_padded = torch.LongTensor(B, max_text_len)
        wav_padded = torch.FloatTensor(B, 1, wav_lens_max)
        token_padded = token_padded.zero_() + self.pad_id
        wav_padded = wav_padded.zero_() + self.pad_id
        for i in range(len(ids_sorted_decreasing)):
            token_ids = batch["token_ids"][i]
            token_padded[i, : batch["token_len"][i]] = torch.LongTensor(token_ids)

            wav = batch["wav"][i]
            wav_padded[i, :, : wav.size(1)] = torch.FloatTensor(wav)
        return {
            "tokens": token_padded,
            "token_lens": token_lens,
            "token_rel_lens": token_rel_lens,
            "waveform": wav_padded,  # (B x T)
            "waveform_lens": wav_lens,  # (B)
            "waveform_rel_lens": wav_rel_lens,
            "speaker_names": batch["speaker_name"],
            "language_names": batch["language_name"],
            "audio_files": batch["wav_file"],
            "raw_text": batch["raw_text"],
            "audio_unique_names": batch["audio_unique_name"],
            "pitch": pitch
        }


##############################
# MODEL DEFINITION
##############################


@dataclass
class Naturalspeech2Args(Coqpit):
    """NaturalSpeech2 model arguments.

    Args:
    """
    speaker_embedding_channels: int = 512
    num_speakers: int=0
    # DurationPredictor params
    dp_hidden_dim: int = 512
    dp_n_layers: int = 9
    dp_n_attentions: int = 3
    dp_attention_head: int = 8
    dp_kernel_size: int = 3
    dp_dropout: float = 0.3

    # PitchPredictor params
    pp_hidden_dim: int = 512
    pp_n_layers: int = 9
    pp_n_attentions: int = 3
    pp_attention_head: int = 8
    pp_kernel_size: int = 3
    pp_dropout: float = 0.5

    # PromptEncoder params
    pre_hidden_dim: int = 512
    pre_nhead: int = 8
    pre_n_layers: int = 4
    pre_dim_feedforward: int = 1024
    pre_kernel_size: int = 9
    pre_dropout: float = 0.3

    # PhonemeEncoder params
    num_chars: int = 150
    phe_hidden_dim: int = 512
    phe_nhead: int = 8
    phe_n_layers: int = 4
    phe_dim_feedforward: int = 1024
    phe_kernel_size: int = 9
    phe_dropout: float = 0.3

    # Diffusion params
    max_step: int = 1000
    diff_size: int = 512
    audio_codec_size: int = 512
    pre_attention_query_token: int = 32
    pre_attention_query_size: int = 512
    pre_attention_head: int = 8
    wavenet_kernel_size: int = 3
    wavenet_dilation: int = 2
    wavenet_stack: int = 40
    wavenet_dropout_rate: float = 0.2
    wavenet_attention_apply_in_stack: int = 3
    wavenet_attention_head: int = 8
    noise_schedule: str = "sigmoid"
    diff_segment_size: int = 48

    # Freeze layers
    freeze_phoneme_encoder: bool = False
    freeze_prompt_encoder: bool = False
    freeze_duration_predictor: bool = False
    freeze_pitch_predictor: bool = False
    freeze_diffusion: bool = False


class Naturalspeech2(BaseTTS):
    """NaturalSpeech2 TTS model

    Paper::
        https://arxiv.org/pdf/2304.09116.pdf

    Paper Abstract::
        Scaling text-to-speech (TTS) to large-scale, multi-speaker, and in-the-wild datasets
        is important to capture the diversity in human speech such as speaker identities,
        prosodies, and styles (e.g., singing). Current large TTS systems usually quantize
        speech into discrete tokens and use language models to generate these tokens one
        by one, which suffer from unstable prosody, word skipping/repeating issue, and
        poor voice quality. In this paper, we develop NaturalSpeech 2, a TTS system
        that leverages a neural audio codec with residual vector quantizers to get the
        quantized latent vectors and uses a diffusion model to generate these latent vectors
        conditioned on text input. To enhance the zero-shot capability that is important
        to achieve diverse speech synthesis, we design a speech prompting mechanism to
        facilitate in-context learning in the diffusion model and the duration/pitch predictor.
        We scale NaturalSpeech 2 to large-scale datasets with 44K hours of speech and
        singing data and evaluate its voice quality on unseen speakers. NaturalSpeech 2
        outperforms previous TTS systems by a large margin in terms of prosody/timbre
        similarity, robustness, and voice quality in a zero-shot setting, and performs novel
        zero-shot singing synthesis with only a speech prompt. Audio samples are available
        at https://speechresearch.github.io/naturalspeech2.

    Check :class:`TTS.tts.configs.naturalspeech2_config.NaturalSpeech2Config` for class arguments.

    Examples:
        >>> from TTS.tts.configs.v import NaturalSpeech2Config
        >>> from TTS.tts.models.naturalspeech2 import NaturalSpeech2
        >>> config = NaturalSpeech2Config()
        >>> model = NaturalSpeech2(config)
    """

    def __init__(
        self,
        config: Coqpit,
        ap: "AudioProcessor" = None,
        tokenizer: "TTSTokenizer" = None,
        speaker_manager: SpeakerManager = None,
        language_manager: LanguageManager = None,
    ):
        super().__init__(config, ap, tokenizer, speaker_manager)
        self.hificodec = VQVAE(
            config_path= '/root/Desktop/Naturalspeech2/TTS/TTS/tts/layers/naturalspeech2/hificodec/config_24k_320d.json',
            ckpt_path= '/root/Desktop/Naturalspeech2/TTS/TTS/tts/layers/naturalspeech2/hificodec/HiFi-Codec-24k-320d',
            with_encoder= True
            ).eval().to('cuda')
        # self.hificodec = VQVAE(
        #     config_path= '/root/Desktop/Naturalspeech2/TTS/TTS/tts/layers/naturalspeech2/hificodec/config_16k_320d.json',
        #     ckpt_path= '/root/Desktop/Naturalspeech2/TTS/TTS/tts/layers/naturalspeech2/hificodec/HiFi-Codec-16k-320d-large-universal',
        #     with_encoder= True
        #     ).eval().to('cuda')
        self.init_multispeaker(config)
        self.embedded_language_dim = 0
        self.diff_segment_size = self.args.diff_segment_size
        self.phoneme_encoder = TransformerEncoder(
            self.args.phe_hidden_dim,
            self.args.phe_nhead,
            self.args.phe_n_layers,
            self.args.phe_dim_feedforward,
            self.args.phe_kernel_size,
            self.args.phe_dropout,
            n_vocab=self.args.num_chars,
            encoder_type="phoneme"
        )

        self.prompt_encoder = TransformerEncoder(
            self.args.pre_hidden_dim,
            self.args.pre_nhead,
            self.args.pre_n_layers,
            self.args.pre_dim_feedforward,
            self.args.pre_kernel_size,
            self.args.pre_dropout,
            max_len=5000,
            encoder_type="prompt",
        )

        self.duration_predictor = ConvBlockWithPrompting(
            self.args.dp_hidden_dim,
            self.args.dp_n_layers,
            self.args.dp_n_attentions,
            self.args.dp_attention_head,
            self.args.dp_kernel_size,
            self.args.dp_dropout,
            "duration"
        )

        self.pitch_predictor = ConvBlockWithPrompting(
            self.args.pp_hidden_dim,
            self.args.pp_n_layers,
            self.args.pp_n_attentions,
            self.args.pp_attention_head,
            self.args.pp_kernel_size,
            self.args.pp_dropout,
            "pitch"
        )
        self.pitch_emb = nn.Embedding(256, self.args.pp_hidden_dim)
        # self.prior_net = ConvBlockWithPrompting(
        #     self.args.pr_hidden_dim,
        #     self.args.pr_n_layers,
        #     self.args.pr_n_attentions,
        #     self.args.pr_attention_head,
        #     self.args.pr_kernel_size,
        #     self.args.pr_dropout,
        #     "prior"
        # )

        # self.combination_network = nn.Sequential(
        #     nn.Linear(1024, 256),
        #     nn.ReLU(),
        #     nn.Linear(256, 512)
        # )

        self.diffusion = Diffusion(
            max_step=self.args.max_step,
            audio_codec_size=self.args.audio_codec_size,
            size_=self.args.diff_size,
            pre_attention_query_token=self.args.pre_attention_query_token,
            pre_attention_query_size=self.args.pre_attention_query_size,
            pre_attention_head=self.args.pre_attention_head,
            wavenet_kernel_size=self.args.wavenet_kernel_size,
            wavenet_dilation=self.args.wavenet_dilation,
            wavenet_stack=self.args.wavenet_stack,
            wavenet_dropout_rate=self.args.wavenet_dropout_rate,
            wavenet_attention_apply_in_stack=self.args.wavenet_attention_apply_in_stack,
            wavenet_attention_head=self.args.wavenet_attention_head,
            noise_schedule=self.args.noise_schedule,
            scale=1.0,
        )

        self.aligner = AlignmentNetwork(in_query_channels=80, in_key_channels=self.args.phe_hidden_dim)

    @property
    def device(self):
        return next(self.parameters()).device
    
    def init_multispeaker(self, config: Coqpit):
        """Initialize multi-speaker modules of a model. A model can be trained either with a speaker embedding layer
        or with external `d_vectors` computed from a speaker encoder model.

        You must provide a `speaker_manager` at initialization to set up the multi-speaker modules.

        Args:
            config (Coqpit): Model configuration.
            data (List, optional): Dataset items to infer number of speakers. Defaults to None.
        """
        self.embedded_speaker_dim = 0
        self.num_speakers = self.args.num_speakers

        if self.speaker_manager:
            self.num_speakers = self.speaker_manager.num_speakers

        self._init_speaker_embedding()

    def _init_speaker_embedding(self):
        # pylint: disable=attribute-defined-outside-init
        if self.num_speakers > 0:
            print(" > initialization of speaker-embedding layers.")
            self.embedded_speaker_dim = self.args.speaker_embedding_channels
            self.emb_g = nn.Embedding(self.num_speakers, self.embedded_speaker_dim)

    def init_multilingual(self, config: Coqpit):
        """Initialize multilingual modules of a model.

        Args:
            config (Coqpit): Model configuration.
        """
        if self.args.language_ids_file is not None:
            self.language_manager = LanguageManager(language_ids_file_path=config.language_ids_file)

        if self.language_manager:
            print(" > initialization of language-embedding layers.")
            self.num_languages = self.language_manager.num_languages
            self.embedded_language_dim = self.args.embedded_language_dim
            self.emb_l = nn.Embedding(self.num_languages, self.embedded_language_dim)
            torch.nn.init.xavier_uniform_(self.emb_l.weight)
        else:
            self.embedded_language_dim = 0

    def on_train_step_start(self, trainer):
        """Schedule binary loss weight."""
        self._freeze_layers()

    def _freeze_layers(self):
        if self.args.freeze_phoneme_encoder:
            for param in self.phoneme_encoder.parameters():
                param.requires_grad = False

        if self.args.freeze_prompt_encoder:
            for param in self.prompt_encoder.parameters():
                param.requires_grad = False

        if self.args.freeze_duration_predictor:
            for param in self.duration_predictor.parameters():
                param.requires_grad = False

        if self.args.freeze_pitch_predictor:
            for param in self.pitch_predictor.parameters():
                param.requires_grad = False

        if self.args.freeze_diffusion:
            for param in self.diffusion.parameters():
                param.requires_grad = False

    def _forward_aligner(
        self, x: torch.FloatTensor, y: torch.FloatTensor, x_mask: torch.IntTensor, y_mask: torch.IntTensor
    ) -> Tuple[torch.IntTensor, torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """Aligner forward pass.

        1. Compute a mask to apply to the attention map.
        2. Run the alignment network.
        3. Apply MAS to compute the hard alignment map.
        4. Compute the durations from the hard alignment map.

        Args:
            x (torch.FloatTensor): Input sequence.
            y (torch.FloatTensor): Output sequence.
            x_mask (torch.IntTensor): Input sequence mask.
            y_mask (torch.IntTensor): Output sequence mask.

        Returns:
            Tuple[torch.IntTensor, torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
                Durations from the hard alignment map, soft alignment potentials, log scale alignment potentials,
                hard alignment map.

        Shapes:
            - x: :math:`[B, T_en, C_en]`
            - y: :math:`[B, T_de, C_de]`
            - x_mask: :math:`[B, 1, T_en]`
            - y_mask: :math:`[B, 1, T_de]`

            - alignment_hard: :math:`[B, T_en]`
            - alignment_soft: :math:`[B, T_en, T_de]`
            - alignment_logprob: :math:`[B, 1, T_de, T_en]`
            - alignment_mas: :math:`[B, T_en, T_de]`
        """
        attn_mask = torch.unsqueeze(x_mask, -1) * torch.unsqueeze(y_mask, 2)
        alignment_soft, alignment_logprob = self.aligner(y.transpose(1, 2), x, x_mask, None)
        assert not torch.isnan(alignment_soft).any()
        alignment_mas = maximum_path(
            alignment_soft.squeeze(1).transpose(1, 2).contiguous(), attn_mask.squeeze(1).contiguous()
        )
        alignment_hard = torch.sum(alignment_mas, -1).float()
        alignment_soft = alignment_soft.squeeze(1).transpose(1, 2)
        return alignment_hard, alignment_soft, alignment_logprob, alignment_mas

    @staticmethod
    def _set_cond_input(aux_input: Dict):
        # [TODO] use this
        return None
    
    @staticmethod
    def generate_attn(dr, x_mask, y_mask=None):
        """Generate an attention mask from the durations.

        Shapes
           - dr: :math:`(B, T_{en})`
           - x_mask: :math:`(B, T_{en})`
           - y_mask: :math:`(B, T_{de})`
        """
        # compute decode mask from the durations
        if y_mask is None:
            y_lengths = dr.sum(1).long()
            y_lengths[y_lengths < 1] = 1
            y_mask = torch.unsqueeze(sequence_mask(y_lengths, None), 1).to(dr.dtype)
        attn_mask = torch.unsqueeze(x_mask, -1) * torch.unsqueeze(y_mask, 2)
        attn = generate_path(dr, attn_mask.squeeze(1)).to(dr.dtype)
        return attn

    def expand_encodings(self, phoneme_enc, attn, pitch):
        expanded_dur = torch.einsum("klmn, kjm -> kjn", [attn, phoneme_enc])
        # expanded_pitch = torch.einsum("klmn, kjm -> kjn", [attn, pitch])
        # expanded_pitch = expanded_pitch.expand(-1, expanded_dur.shape[1], -1)  # Now shape is (batch, time, 512)
        pitch_emb = self.pitch_emb(f0_to_coarse(pitch).squeeze(1)).transpose(1,2)
        expanded_pitch = torch.einsum("klmn, kjm -> kjn", [attn, pitch_emb])
        expanded_encodings = expanded_dur + expanded_pitch
        # expanded_encodings = expanded_dur * expanded_pitch
        return expanded_encodings

    def forward(  # pylint: disable=dangerous-default-value
        self,
        tokens: torch.tensor,
        tokens_lens: torch.tensor,
        latents: torch.tensor,
        latents_lengths: torch.tensor,
        mel: torch.tensor,
        mel_lens: torch.tensor,
        pitch: torch.tensor,
        aux_input={"prompt": None, "durations": None, "language_ids": None, "speaker_ids": None},
    ) -> Dict:
        outputs = {}
        g = None
        if "speaker_ids" in aux_input and aux_input["speaker_ids"] is not None:
            sid = aux_input["speaker_ids"]
            if sid.ndim == 0:
                sid = sid.unsqueeze_(0)
            g = self.emb_g(sid).unsqueeze(-1)
        tokens_mask = torch.unsqueeze(sequence_mask(tokens_lens, tokens.shape[1]), 1).float()        
        
        phoneme_enc = self.phoneme_encoder(tokens.unsqueeze(2), tokens_mask, g=g).transpose(1, 2)
        mel_mask = torch.unsqueeze(sequence_mask(mel_lens, None), 1).float()

        p_size = random.randint(self.diff_segment_size-16, self.diff_segment_size+16)
        if torch.min(latents_lengths).item() > p_size:
            seg_size_std = p_size
        else:
            seg_size_std = int(torch.min(latents_lengths).item() * 0.5)
        
        speech_prompts, segment_indices = rand_segments(
            latents, latents_lengths, seg_size_std , let_short_samples=True, pad_short=True
        )

        remaining_mask = torch.ones_like(latents, dtype=torch.bool)
        for i in range(latents.size(0)):
            remaining_mask[i, :, segment_indices[i] : segment_indices[i] + seg_size_std] = 0

        # Encode speech prompt
        speech_prompts_enc = self.prompt_encoder(speech_prompts).transpose(1,2)

        alignment_hard, alignment_soft, alignment_logprob, alignment_mas = self._forward_aligner(
            phoneme_enc, mel.transpose(1, 2), tokens_mask, mel_mask
        )

        alignment_soft = alignment_soft.transpose(1, 2)
        durations_pred = self.duration_predictor(phoneme_enc, speech_prompts_enc, tokens_mask)
        durations_pred = durations_pred.squeeze(1)
        pitch = average_over_durations(pitch, alignment_hard)
        pitch_pred = self.pitch_predictor(phoneme_enc, speech_prompts_enc, tokens_mask)
        expanded_encodings = self.expand_encodings(phoneme_enc, alignment_mas.unsqueeze(1), pitch)

        out_len = torch.stack([
                duration.sum() + 1
                for duration in alignment_hard
                ], dim= 0)

        diffusion_targets, diffusion_predictions, diffusion_starts, loss_weight = self.diffusion(
            latents=latents,
            encodings=expanded_encodings,
            lengths=out_len,
            speech_prompts=speech_prompts_enc,
        )
        latents_hat = diffusion_starts.masked_select(remaining_mask).view(diffusion_starts.shape[0], diffusion_starts.shape[1], -1)
        # linear_latent = linear_latent.masked_select(remaining_mask).view(linear_latent.shape[0], linear_latent.shape[1], -1)

        # predictions = self.hificodec.generator(latents_hat)
        
        outputs.update(
            {
                "input_lens": tokens_lens,
                "spec_lens": mel_lens,
                # "linear_latent": None,
                "diffusion_targets": diffusion_targets,
                "diffusion_predictions": diffusion_predictions,
                "latent_hat": latents_hat,
                "speech_prompts": speech_prompts,
                "durations": alignment_hard,
                "durations_pred": durations_pred,
                "pitch": 2595. * torch.log10(1. + pitch / 700.) / 500,
                "pitch_pred": pitch_pred,
                "alignment_hard": alignment_mas.transpose(1,2),
                "alignment_logprob": alignment_logprob,
                "segment_indices": segment_indices,
                "remaining_mask": remaining_mask,
                "loss_weight": loss_weight
            }
        )

        return outputs
    def remove_spikes(self, tensor, window_size):
        # Make sure the tensor has the right size
        assert len(tensor.shape) == 3, "Tensor must be of size [1, 1, time]"

        # Make sure the window size is odd
        assert window_size % 2 == 1, "Window size must be odd"

        # Pad the tensor to handle edges using 'replicate' mode
        pad_size = window_size // 2
        padded_tensor = torch.nn.functional.pad(tensor, (pad_size, pad_size), mode='replicate')

        # Apply the median filter
        result = torch.zeros_like(tensor)
        for i in range(tensor.size(2)):
            result[0, 0, i] = padded_tensor[0, 0, i:i+window_size].median()

        return result
    @torch.no_grad()
    def inference(self, x, aux_input={"style_mel": None, "speaker_ids": None}):  # pylint: disable=dangerous-default-value
        outputs = {}
        g = None
        if "speaker_ids" in aux_input and aux_input["speaker_ids"] is not None:
            sid = aux_input["speaker_ids"]
            if sid.ndim == 0:
                sid = sid.unsqueeze_(0)
            g = self.emb_g(sid).unsqueeze(-1)
        voice_prompt = aux_input["style_mel"].unsqueeze(0)
        print(voice_prompt.shape)
        with torch.no_grad():
            voice_prompt = self.hificodec.encoder(voice_prompt.unsqueeze(0))
            # voice_prompt = self.hificodec.quantizer.embed(codes) # type: ignore
        print(voice_prompt.shape)
        # Encode speech prompt
        speech_prompts_enc = self.prompt_encoder(voice_prompt).transpose(1, 2)

        #Encode Phonemes
        tokens_lens = torch.tensor(x.shape[1:2]).to(x.device)
        # print(tokens_lens)
        # print(x.shape)
        tokens_mask = torch.unsqueeze(sequence_mask(tokens_lens, x.shape[1]), 1).float()
        phoneme_enc = self.phoneme_encoder(x.unsqueeze(2), g=g).transpose(1, 2)
        
        #duration predict
        durations_pred = self.duration_predictor(phoneme_enc, speech_prompts_enc)
        # print(durations_pred)
        durations_pred = torch.round(durations_pred) * 1.0

        attn = self.generate_attn(durations_pred.squeeze(0), tokens_mask.squeeze(0))
        attn = attn.unsqueeze(1)
        # print(durations_pred)
        pitch_pred = self.pitch_predictor(phoneme_enc, speech_prompts_enc)
        pitch_pred = (700 * (torch.pow(10, pitch_pred * 500 / 2595) - 1))
        # print(pitch_pred)
        expanded_encodings = self.expand_encodings(phoneme_enc, attn, pitch_pred)

        out_len = torch.stack([
                duration.sum() + 1
                for duration in durations_pred
                ], dim = 0)
        latents = self.diffusion.ddpm(
            encodings=expanded_encodings,
            lengths=out_len,
            speech_prompts=speech_prompts_enc,
            ddim_steps=150)
        with torch.no_grad():
            *_, latents = self.hificodec.quantizer(latents)
            latents = [code.reshape(x.size(0), -1) for code in latents]
            latents = torch.stack(latents,-1)
            wav = self.hificodec(latents).squeeze(1)
            # wav = self.hificodec.generator(latents).squeeze(1)
        print(wav.squeeze(1).shape)
        outputs["model_outputs"] = wav.squeeze(1)
        outputs["durations"] = durations_pred
        outputs["alignments"] = attn
        outputs["pitch"] = pitch_pred
        return outputs

    def train_step(self, batch: dict, criterion: nn.Module) -> Tuple[Dict, Dict]:
        latents_lens = batch["latents_lens"]
        latents = batch["latents"]
        # codes = batch["codes"]
        tokens = batch["tokens"]
        token_lenghts = batch["token_lens"]
        language_ids = batch["language_ids"]
        speaker_ids = batch["speaker_ids"]
        waveform = batch["waveform"]
        mel_lens = batch["mel_lens"]
        mel = batch["mel"]
        pitch = batch["pitch"]
 
        outputs = self.forward(
            tokens,
            token_lenghts,
            latents,
            latents_lens,
            mel,
            mel_lens,
            pitch,
            aux_input={"speaker_ids": speaker_ids}
        )

        latents = latents.masked_select(outputs["remaining_mask"]).view(latents.shape[0], latents.shape[1], -1)
        outputs["latents"] = latents
 
        # ce_loss = None
        # if self.config.upsampler_loss_alpha > 0:
        #     _,q_loss,_ = self.hificodec.quantizer(latents)
        #     _,q_loss_hat,_ = self.hificodec.quantizer(outputs["latent_hat"])
        
        # compute losses
        with autocast(enabled=False):  # use float32 for the criterion
            loss_dict = criterion(
                q_loss=None,
                q_loss_hat=None,
                duration = outputs["durations"],
                duration_pred=outputs["durations_pred"],
                pitch=outputs["pitch"],
                pitch_pred=outputs["pitch_pred"],
                latents=latents,
                diffusion_targets=outputs["diffusion_targets"],
                latent_z_hat=outputs["latent_hat"],
                diffusion_predictions=outputs["diffusion_predictions"],
                input_lens=token_lenghts,
                spec_lens=latents_lens,
                alignment_logprob=outputs["alignment_logprob"],
                diff_loss_weight=outputs["loss_weight"]
            )

        return outputs, loss_dict

    def _log(self, ap, batch, outputs, name_prefix="train"):  # pylint: disable=unused-argument,no-self-use
        # y_hat = outputs["audio_hat"]
        # with torch.no_grad():
        #     audio_slice = self.hificodec.generator(outputs["latents"]).squeeze(1)
        # y = audio_slice
        # figures = plot_results(y_hat, y, ap, name_prefix)
        # sample_voice = y_hat[0].squeeze(0).detach().cpu().numpy()
        # audios = {f"{name_prefix}/audio": sample_voice}
        # figures.update({
        #         "pitch": plot_avg_pitch(outputs["pitch"][0].detach().cpu().numpy(), batch["raw_text"][0])
        #     }
        # )
        alignments = outputs["alignment_hard"]
        align_img = alignments[0]
        figures = { "alignment": plot_alignment(align_img, output_fig=False) }
        return figures

    def train_log(
        self, batch: dict, outputs: dict, logger: "Logger", assets: dict, steps: int
    ):  # pylint: disable=no-self-use
        """Create visualizations and waveform examples.

        For example, here you can plot spectrograms and generate sample sample waveforms from these spectrograms to
        be projected onto Tensorboard.

        Args:
            ap (AudioProcessor): audio processor used at training.
            batch (Dict): Model inputs used at the previous training step.
            outputs (Dict): Model outputs generated at the previoud training step.

        Returns:
            Tuple[Dict, np.ndarray]: training plots and output waveform.
        """
        figures = self._log(self.ap, batch, outputs, "train")
        logger.train_figures(steps, figures)
        # logger.train_audios(steps, audios, self.ap.sample_rate)

    @torch.no_grad()
    def eval_step(self, batch: dict, criterion: nn.Module):
        return self.train_step(batch, criterion)

    def eval_log(self, batch: dict, outputs: dict, logger: "Logger", assets: dict, steps: int) -> None:
        figures = self._log(self.ap, batch, outputs, "eval")
        logger.eval_figures(steps, figures)
        # logger.eval_audios(steps, audios, self.ap.sample_rate)

    def get_aux_input_from_test_sentences(self, sentence_info):
        if hasattr(self.config, "model_args"):
            config = self.config.model_args
        else:
            config = self.config

        # extract speaker and language info
        text, voice_prompt, language_name = None, None, None

        if isinstance(sentence_info, list):
            if len(sentence_info) == 1:
                text = sentence_info[0]
            elif len(sentence_info) == 2:
                text, voice_prompt = sentence_info
        else:
            text = sentence_info

        return {
            "text": text,
            "voice_prompt": voice_prompt
        }

    @torch.no_grad()
    def test_run(self, assets) -> Tuple[Dict, Dict]:
        """Generic test run for `tts` models used by `Trainer`.

        You can override this for a different behaviour.

        Returns:
            Tuple[Dict, Dict]: Test figures and audios to be projected to Tensorboard.
        """
        print(" | > Synthesizing test sentences.")
        test_audios = {}
        test_figures = {}
        test_sentences = self.config.test_sentences
        for idx, s_info in enumerate(test_sentences):
            aux_inputs = self.get_aux_input_from_test_sentences(s_info)
            wav, alignment, _, _ = synthesis(
                self,
                aux_inputs["text"],
                self.config,
                "cuda" in str(next(self.parameters()).device),
                style_wav=aux_inputs["voice_prompt"],
                use_griffin_lim=True,
                do_trim_silence=False,
            ).values()
            test_audios["{}-audio".format(idx)] = wav
            wav_pl = torch.from_numpy(wav).unsqueeze(0)
            # spec = wav_to_spec(wav_pl.unsqueeze(0), self.ap.fft_size, self.ap.hop_length, self.ap.win_length, center=False)
            spec = wav_to_mel(wav_pl.unsqueeze(0), self.ap.fft_size, 80, self.ap.sample_rate, self.ap.hop_length, 
                              self.ap.win_length, fmin=self.config.audio.mel_fmin,fmax=self.config.audio.mel_fmax, center=False)
            test_figures["{}-spectrogram".format(idx)] = plot_spectrogram(spec.mT, output_fig=False)
            test_figures["{}-alignment".format(idx)] = plot_alignment(alignment.mT, output_fig=False)
        return {"figures": test_figures, "audios": test_audios}

    def test_log(
        self, outputs: dict, logger: "Logger", assets: dict, steps: int  # pylint: disable=unused-argument
    ) -> None:
        logger.test_audios(steps, outputs["audios"], self.ap.sample_rate)
        logger.test_figures(steps, outputs["figures"])

    def min_max_scaler(self, tensor):
        min_val = tensor.min()
        max_val = tensor.max()
        tensor_norm = (tensor - min_val) / (max_val - min_val)
        return tensor_norm, min_val, max_val

    def min_max_inverse_scaler(self, tensor_norm, min_val, max_val):
        tensor = tensor_norm * (max_val - min_val) + min_val
        return tensor

    def format_batch(self, batch: Dict) -> Dict:
        """Compute langugage IDs and codec for the batch if necessary."""
        language_ids = None
        speaker_ids = None
        # get language ids from language names
        if self.language_manager is not None and self.language_manager.name_to_id:
            language_ids = [self.language_manager.name_to_id[ln] for ln in batch["language_names"]]
        # get numerical speaker ids from speaker names
        if self.speaker_manager is not None and self.speaker_manager.name_to_id:
            speaker_ids = [self.speaker_manager.name_to_id[sn] for sn in batch["speaker_names"]]

        if language_ids is not None:
            language_ids = torch.LongTensor(language_ids)
        if speaker_ids is not None:
            speaker_ids = torch.LongTensor(speaker_ids)
        
        batch["language_ids"] = language_ids
        batch["speaker_ids"] = speaker_ids
        return batch

    def format_batch_on_device(self, batch):
        """Compute spectrograms on the device."""
        ac = self.config.audio

        wav = batch["waveform"]
        waveform_lens = batch["waveform_lens"]
        # print(waveform_lens)

        with torch.no_grad():
            latents = self.hificodec.encoder(wav)
            # latents = self.hificodec.quantizer.embed(codes) # type: ignore

        batch["latents"] = latents
        # batch["codes"] = codes.squeeze(1)
        
        # Compute spectrograms
        batch["spec"] = wav_to_spec(wav, ac.fft_size, ac.hop_length, ac.win_length, center=False)

        spec_mel = batch["spec"]
        batch["mel"] = spec_to_mel(
            spec=spec_mel,
            n_fft=ac.fft_size,
            num_mels=ac.num_mels,
            sample_rate=ac.sample_rate,
            fmin=ac.mel_fmin,
            fmax=ac.mel_fmax,
        )

        # # Normalise latents with min-max scaler and store min and max values
        # _, latents_min, latents_max = self.min_max_scaler(batch["latents"])
        # batch["latents_min"] = latents_min
        # batch["latents_max"] = latents_max
        # Padding adjustments
        # print(batch["latents"].shape[-1], batch["mel"].shape[-1])
        if batch["latents"].shape[-1] < batch["mel"].shape[-1]:
            diff = batch["mel"].shape[-1] - batch["latents"].shape[-1]
            batch["latents"] = torch.nn.functional.pad(batch["latents"], (0, diff), mode='constant', value=0)
        elif batch["latents"].shape[-1] > batch["mel"].shape[-1]:
            diff = batch["latents"].shape[-1] - batch["mel"].shape[-1]
            batch["mel"] = torch.nn.functional.pad(batch["mel"], (0, diff), mode='constant', value=0)

        assert batch["latents"].shape[2] == batch["mel"].shape[2], f"{batch['latents'].shape[2]}, {batch['mel'].shape[2]}"

        # Compute spectrogram frame lengths
        batch["mel_lens"] = (batch["mel"].shape[2] * batch["waveform_rel_lens"]).int()
        batch["latents_lens"] = (batch["latents"].shape[2] * batch["waveform_rel_lens"]).int()

        assert (batch["latents_lens"] - batch["mel_lens"]).sum() == 0

        # Zero the padding frames
        batch["mel"] = batch["mel"] * sequence_mask(batch["mel_lens"]).unsqueeze(1)
        batch["latents"] = batch["latents"] * sequence_mask(batch["latents_lens"]).unsqueeze(1)
        return batch


    def get_sampler(self, config: Coqpit, dataset: TTSDataset, num_gpus=1, is_eval=False):
        weights = None
        data_items = dataset.samples
        if getattr(config, "use_weighted_sampler", False):
            for attr_name, alpha in config.weighted_sampler_attrs.items():
                print(f" > Using weighted sampler for attribute '{attr_name}' with alpha '{alpha}'")
                multi_dict = config.weighted_sampler_multipliers.get(attr_name, None)
                print(multi_dict)
                weights, attr_names, attr_weights = get_attribute_balancer_weights(
                    attr_name=attr_name, items=data_items, multi_dict=multi_dict
                )
                weights = weights * alpha
                print(f" > Attribute weights for '{attr_names}' \n | > {attr_weights}")

        # input_audio_lenghts = [os.path.getsize(x["audio_file"]) for x in data_items]

        if weights is not None:
            w_sampler = WeightedRandomSampler(weights, len(weights))
            batch_sampler = BucketBatchSampler(
                w_sampler,
                data=data_items,
                batch_size=config.eval_batch_size if is_eval else config.batch_size,
                sort_key=lambda x: os.path.getsize(x["audio_file"]),
                drop_last=True,
            )
        else:
            batch_sampler = None
        # sampler for DDP
        if batch_sampler is None:
            batch_sampler = DistributedSampler(dataset) if num_gpus > 1 else None
        else:  # If a sampler is already defined use this sampler and DDP sampler together
            batch_sampler = (
                DistributedSamplerWrapper(batch_sampler) if num_gpus > 1 else batch_sampler
            )  # TODO: check batch_sampler with multi-gpu
        return batch_sampler

    def get_data_loader(
        self,
        config: Coqpit,
        assets: Dict,
        is_eval: bool,
        samples: Union[List[Dict], List[List]],
        verbose: bool,
        num_gpus: int,
        rank: int = None,
    ) -> "DataLoader":
        if is_eval and not config.run_eval:
            loader = None
        else:
            # init dataloader
            dataset = Naturalspeech2Dataset(
                model_args=self.args,
                samples=samples,
                ap=self.ap,
                batch_group_size=0 if is_eval else config.batch_group_size * config.batch_size,
                min_text_len=config.min_text_len,
                max_text_len=config.max_text_len,
                min_audio_len=config.min_audio_len,
                max_audio_len=config.max_audio_len,
                phoneme_cache_path=config.phoneme_cache_path,
                precompute_num_workers=config.precompute_num_workers,
                verbose=verbose,
                tokenizer=self.tokenizer,
                start_by_longest=config.start_by_longest,
                compute_f0=config.compute_f0,
                f0_cache_path=config.f0_cache_path,
            )

            # wait all the DDP process to be ready
            if num_gpus > 1:
                dist.barrier()

            # sort input sequences from short to long
            dataset.preprocess_samples()

            # get samplers
            sampler = self.get_sampler(config, dataset, num_gpus)
            if sampler is None:
                loader = DataLoader(
                    dataset,
                    batch_size=config.eval_batch_size if is_eval else config.batch_size,
                    shuffle=False,  # shuffle is done in the dataset.
                    collate_fn=dataset.collate_fn,
                    drop_last=False,  # setting this False might cause issues in AMP training.
                    num_workers=config.num_eval_loader_workers if is_eval else config.num_loader_workers,
                    pin_memory=False,
                )
            else:
                if num_gpus > 1:
                    loader = DataLoader(
                        dataset,
                        sampler=sampler,
                        batch_size=config.eval_batch_size if is_eval else config.batch_size,
                        collate_fn=dataset.collate_fn,
                        num_workers=config.num_eval_loader_workers if is_eval else config.num_loader_workers,
                        pin_memory=False,
                    )
                else:
                    loader = DataLoader(
                        dataset,
                        batch_sampler=sampler,
                        collate_fn=dataset.collate_fn,
                        num_workers=config.num_eval_loader_workers if is_eval else config.num_loader_workers,
                        pin_memory=False,
                    )
        return loader

    def get_criterion(self):
        """Get criterions for each optimizer. The index in the output list matches the optimizer idx used in
        `train_step()`"""
        from TTS.tts.layers.losses import Naturalspeech2Loss  # pylint: disable=import-outside-toplevel

        return Naturalspeech2Loss(self.config)

    def load_checkpoint(
        self, config, checkpoint_path, eval=False, strict=True, cache=False
    ):  # pylint: disable=unused-argument, redefined-builtin
        """Load the model checkpoint and setup for training or inference"""
        state = load_fsspec(checkpoint_path, map_location=torch.device("cpu"), cache=cache)
        # load the model weights
        self.load_state_dict(state["model"], strict=strict)

        if eval:
            self.eval()
            assert not self.training

    @staticmethod
    def init_from_config(config: "NaturalSpeech2Config", samples: Union[List[List], List[Dict]] = None, verbose=True):
        """Initiate model from config

        Args:
            config (NaturalSpeech2Config): Model config.
            samples (Union[List[List], List[Dict]]): Training samples to parse audios for training.
                Defaults to None.
        """
        from TTS.utils.audio import AudioProcessor

        ap = AudioProcessor.init_from_config(config, verbose=verbose)
        tokenizer, new_config = TTSTokenizer.init_from_config(config)
        speaker_manager = SpeakerManager.init_from_config(config, samples)
        language_manager = LanguageManager.init_from_config(config)

        return Naturalspeech2(new_config, ap, tokenizer,speaker_manager, language_manager)


##################################
# NaturalSpeech2 CHARACTERS
##################################


class Naturalspeech2Characters(BaseCharacters):
    """Characters class for NaturalSpeech2 model for compatibility with pre-trained models"""

    def __init__(
        self,
        graphemes: str = _characters,
        punctuations: str = _punctuations,
        pad: str = _pad,
        ipa_characters: str = _phonemes,
    ) -> None:
        if ipa_characters is not None:
            graphemes += ipa_characters
        super().__init__(graphemes, punctuations, pad, None, None, "<BLNK>", is_unique=False, is_sorted=True)

    def _create_vocab(self):
        self._vocab = [self._pad] + list(self._punctuations) + list(self._characters) + [self._blank]
        self._char_to_id = {char: idx for idx, char in enumerate(self.vocab)}
        # pylint: disable=unnecessary-comprehension
        self._id_to_char = {idx: char for idx, char in enumerate(self.vocab)}

    @staticmethod
    def init_from_config(config: Coqpit):
        if config.characters is not None:
            _pad = config.characters["pad"]
            _punctuations = config.characters["punctuations"]
            _letters = config.characters["characters"]
            _letters_ipa = config.characters["phonemes"]
            return (
                Naturalspeech2Characters(
                    graphemes=_letters, ipa_characters=_letters_ipa, punctuations=_punctuations, pad=_pad
                ),
                config,
            )
        characters = Naturalspeech2Characters()
        new_config = replace(config, characters=characters.to_config())
        return characters, new_config

    def to_config(self) -> "CharactersConfig":
        return CharactersConfig(
            characters=self._characters,
            punctuations=self._punctuations,
            pad=self._pad,
            eos=None,
            bos=None,
            blank=self._blank,
            is_unique=False,
            is_sorted=True,
        )
