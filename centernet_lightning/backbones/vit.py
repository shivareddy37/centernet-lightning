# https://arxiv.org/abs/2010.11929
# https://arxiv.org/abs/2106.10270
# https://github.com/google-research/vision_transformer/blob/main/vit_jax/models_vit.py

from __future__ import annotations

from functools import partial

import numpy as np
import torch
import torch.nn.functional as F
from torch import Tensor, nn

from ..components import LayerScale, StochasticDepth
from centernet_lightning.utils.download import torch_hub_download


class MHA(nn.Module):
    def __init__(self, d_model: int, n_heads: int, bias: bool = True, dropout: float = 0.0) -> None:
        super().__init__()
        self.q_proj = nn.Linear(d_model, d_model, bias)
        self.k_proj = nn.Linear(d_model, d_model, bias)
        self.v_proj = nn.Linear(d_model, d_model, bias)
        self.out_proj = nn.Linear(d_model, d_model, bias)
        self.n_heads = n_heads
        self.dropout = dropout
        self.scale = (d_model // n_heads) ** (-0.5)

    def forward(
        self, q: Tensor, k: Tensor | None = None, v: Tensor | None = None, *, attn_bias: Tensor | None = None
    ) -> Tensor:
        k = q if k is None else k
        v = k if v is None else v
        q = self.q_proj(q).unflatten(-1, (self.n_heads, -1)).transpose(-2, -3)  # (B, n_heads, L, head_dim)
        k = self.k_proj(k).unflatten(-1, (self.n_heads, -1)).transpose(-2, -3)
        v = self.v_proj(v).unflatten(-1, (self.n_heads, -1)).transpose(-2, -3)

        if hasattr(F, "scaled_dot_product_attention"):
            out = F.scaled_dot_product_attention(q, k, v, attn_bias, self.dropout if self.training else 0.0)
        else:
            attn = q @ (k * self.scale).transpose(-1, -2)
            if attn_bias is not None:
                attn = attn + attn_bias
            out = F.dropout(torch.softmax(attn, -1), self.dropout, self.training) @ v

        out = out.transpose(-2, -3).flatten(-2)
        out = self.out_proj(out)
        return out


class MLP(nn.Sequential):
    def __init__(self, in_dim: int, hidden_dim: float, dropout: float = 0.0) -> None:
        super().__init__()
        self.linear1 = nn.Linear(in_dim, hidden_dim)
        self.act = nn.GELU()
        self.linear2 = nn.Linear(hidden_dim, in_dim)
        self.dropout = nn.Dropout(dropout)


class ViTBlock(nn.Module):
    def __init__(
        self,
        d_model: int,
        n_heads: int,
        bias: bool = True,
        mlp_ratio: float = 4.0,
        dropout: float = 0.0,
        layer_scale_init: float | None = None,
        stochastic_depth: float = 0.0,
        norm_eps: float = 1e-6,
        attention: type[nn.Module] | None = None,
    ) -> None:
        if attention is None:
            attention = partial(MHA, d_model, n_heads, bias, dropout)
        super().__init__()
        self.mha = nn.Sequential(
            nn.LayerNorm(d_model, norm_eps),
            attention(),
            LayerScale(d_model, layer_scale_init) if layer_scale_init is not None else nn.Identity(),
            StochasticDepth(stochastic_depth),
        )
        self.mlp = nn.Sequential(
            nn.LayerNorm(d_model, norm_eps),
            MLP(d_model, int(d_model * mlp_ratio), dropout),
            LayerScale(d_model, layer_scale_init) if layer_scale_init is not None else nn.Identity(),
            StochasticDepth(stochastic_depth),
        )

    def forward(self, x: Tensor) -> Tensor:
        x = x + self.mha(x)
        x = x + self.mlp(x)
        return x


class MHAPooling(nn.Module):
    def __init__(
        self, d_model: int, n_heads: int, bias: bool = True, mlp_ratio: float = 4.0, norm_eps: float = 1e-6
    ) -> None:
        super().__init__()
        self.probe = nn.Parameter(torch.zeros(1, 1, d_model))
        self.mha = MHA(d_model, n_heads, bias)
        self.norm = nn.LayerNorm(d_model, norm_eps)
        self.mlp = MLP(d_model, int(d_model * mlp_ratio))

    def forward(self, x: Tensor) -> Tensor:
        x = self.mha(self.probe, x).squeeze(1)
        x = x + self.mlp(self.norm(x))
        return x


class ViT(nn.Module):
    def __init__(
        self,
        d_model: int,
        depth: int,
        n_heads: int,
        patch_size: int,
        img_size: int,
        cls_token: bool = True,
        pool_type: str = "cls_token",
        bias: bool = True,
        mlp_ratio: float = 4.0,
        dropout: float = 0.0,
        layer_scale_init: float | None = None,
        stochastic_depth: float = 0.0,
        norm_eps: float = 1e-6,
    ) -> None:
        assert img_size % patch_size == 0
        super().__init__()
        self.patch_embed = nn.Conv2d(3, d_model, patch_size, patch_size)
        self.cls_token = nn.Parameter(torch.zeros(1, 1, d_model)) if cls_token else None
        self.pe = nn.Parameter(torch.empty(1, (img_size // patch_size) ** 2, d_model))
        nn.init.normal_(self.pe, 0, 0.02)

        self.layers = nn.Sequential()
        for _ in range(depth):
            block = ViTBlock(d_model, n_heads, bias, mlp_ratio, dropout, layer_scale_init, stochastic_depth, norm_eps)
            self.layers.append(block)

        self.norm = nn.LayerNorm(d_model, norm_eps)
        self.pool_type = pool_type
        self.pooler = MHAPooling(d_model, n_heads, bias, mlp_ratio, norm_eps) if pool_type == "mha" else None

    def forward(self, imgs: Tensor) -> Tensor:
        out = self.patch_embed(imgs).flatten(2).transpose(1, 2) + self.pe  # (N, C, H, W) -> (N, H*W, C)
        if self.cls_token is not None:
            out = torch.cat([self.cls_token, out], 1)
        out = self.layers(out)

        if self.pool_type == "cls_token":
            return self.norm(out[:, 0])
        elif self.pool_type == "gap":
            return self.norm(out).mean(1)
        elif self.pool_type == "mha":
            return self.pooler(self.norm(out))
        else:
            raise RuntimeError

    @torch.no_grad()
    def resize_pe(self, size: int, interpolation_mode: str = "bicubic") -> None:
        old_size = int(self.pe.shape[1] ** 0.5)
        new_size = size // self.patch_embed.weight.shape[2]
        pe = self.pe.unflatten(1, (old_size, old_size)).permute(0, 3, 1, 2)
        pe = F.interpolate(pe, (new_size, new_size), mode=interpolation_mode)
        pe = pe.permute(0, 2, 3, 1).flatten(1, 2)
        self.pe = nn.Parameter(pe)

    @staticmethod
    def from_config(variant: str, img_size: int, *, weights: str | None = None) -> ViT:
        variant, patch_size = variant.split("_")

        d_model, depth, n_heads = dict(
            Ti=(192, 12, 3),
            S=(384, 12, 6),
            M=(512, 12, 8),
            B=(768, 12, 12),
            L=(1024, 24, 16),
            H=(1280, 32, 16),
        )[variant]
        patch_size = int(patch_size)
        kwargs = dict()
        if weights == "siglip":
            kwargs.update(cls_token=False, pool_type="mha")

        m = ViT(d_model, depth, n_heads, patch_size, img_size, **kwargs)

        if weights == "augreg":
            assert img_size == 224
            ckpt = {
                ("Ti", 16): "Ti_16-i21k-300ep-lr_0.001-aug_none-wd_0.03-do_0.0-sd_0.0.npz",
                ("S", 32): "S_32-i21k-300ep-lr_0.001-aug_none-wd_0.1-do_0.0-sd_0.0.npz",
                ("S", 16): "S_16-i21k-300ep-lr_0.001-aug_light1-wd_0.03-do_0.0-sd_0.0.npz",
                ("B", 32): "B_32-i21k-300ep-lr_0.001-aug_light1-wd_0.1-do_0.0-sd_0.0.npz",
                ("B", 16): "B_16-i21k-300ep-lr_0.001-aug_medium1-wd_0.1-do_0.0-sd_0.0.npz",
                ("L", 16): "L_16-i21k-300ep-lr_0.001-aug_strong1-wd_0.1-do_0.0-sd_0.0.npz",
            }[(variant, patch_size)]
            m.load_flax_ckpt(f"augreg/{ckpt}")

        elif weights == "siglip":
            ckpt = {
                ("B", 16, 224): "webli_en_b16_224_63724782.npz",
                ("B", 16, 256): "webli_en_b16_256_60500360.npz",
                ("B", 16, 384): "webli_en_b16_384_68578854.npz",
                ("B", 16, 512): "webli_en_b16_512_68580893.npz",
                ("L", 16, 256): "webli_en_l16_256_60552751.npz",
                ("L", 16, 384): "webli_en_l16_384_63634585.npz",
            }[(variant, patch_size, img_size)]
            m.load_flax_ckpt(f"siglip/{ckpt}", big_vision=True, prefix="params/img/")

        elif not weights is None:
            raise ValueError(f"Unsupported weights={weights}")

        return m

    @torch.no_grad()
    def load_flax_ckpt(self, ckpt: str, *, big_vision: bool = False, prefix: str = "") -> None:
        if big_vision:
            # https://github.com/google-research/big_vision
            gcs_bucket = "big_vision"
            mha_norm = "LayerNorm_0"
            mha = "MultiHeadDotProductAttention_0"
            mlp_norm = "LayerNorm_1"
            mlp = "MlpBlock_0"

        else:
            # https://github.com/google-research/vision_transformer
            gcs_bucket = "vit_models"
            mha_norm = "LayerNorm_0"
            mha = "MultiHeadDotProductAttention_1"
            mlp_norm = "LayerNorm_2"
            mlp = "MlpBlock_3"

        path = torch_hub_download(f"https://storage.googleapis.com/{gcs_bucket}/{ckpt}")
        jax_weights = {k[len(prefix) :]: torch.from_numpy(v) for k, v in np.load(path).items() if k.startswith(prefix)}

        if self.cls_token is not None:
            self.cls_token.copy_(jax_weights.pop("cls"))
        if big_vision:
            self.pe.copy_(jax_weights.pop("pos_embedding"))
        else:
            pe = jax_weights.pop("Transformer/posembed_input/pos_embedding")
            self.cls_token.add_(pe[:, 0])
            self.pe.copy_(pe[:, 1:])
        load_flax_conv2d(self.patch_embed, jax_weights, "embedding")
        load_flax_ln(self.norm, jax_weights, "Transformer/encoder_norm")

        for i, layer in enumerate(self.layers):
            load_flax_ln(layer.mha[0], jax_weights, f"Transformer/encoderblock_{i}/{mha_norm}")
            load_flax_mha(layer.mha[1], jax_weights, f"Transformer/encoderblock_{i}/{mha}")
            load_flax_ln(layer.mlp[0], jax_weights, f"Transformer/encoderblock_{i}/{mlp_norm}")
            load_flax_linear(layer.mlp[1].linear1, jax_weights, f"Transformer/encoderblock_{i}/{mlp}/Dense_0")
            load_flax_linear(layer.mlp[1].linear2, jax_weights, f"Transformer/encoderblock_{i}/{mlp}/Dense_1")

        # big_vision only
        if self.pooler is not None:
            self.pooler.probe.copy_(jax_weights.pop("MAPHead_0/probe"))
            load_flax_mha(self.pooler.mha, jax_weights, "MAPHead_0/MultiHeadDotProductAttention_0")
            load_flax_ln(self.pooler.norm, jax_weights, "MAPHead_0/LayerNorm_0")
            load_flax_linear(self.pooler.mlp.linear1, jax_weights, "MAPHead_0/MlpBlock_0/Dense_0")
            load_flax_linear(self.pooler.mlp.linear2, jax_weights, "MAPHead_0/MlpBlock_0/Dense_1")

        if len(jax_weights) > 0:
            print(jax_weights.keys())


def load_flax_ln(norm: nn.LayerNorm, weights: dict[str, Tensor], prefix: str) -> None:
    norm.weight.copy_(weights.pop(f"{prefix}/scale"))
    norm.bias.copy_(weights.pop(f"{prefix}/bias"))


def load_flax_linear(linear: nn.Linear, weights: dict[str, Tensor], prefix: str) -> None:
    linear.weight.copy_(weights.pop(f"{prefix}/kernel").T)
    linear.bias.copy_(weights.pop(f"{prefix}/bias"))


def load_flax_conv2d(conv2d: nn.Conv2d, weights: dict[str, Tensor], prefix: str) -> None:
    conv2d.weight.copy_(weights.pop(f"{prefix}/kernel").permute(3, 2, 0, 1))
    conv2d.bias.copy_(weights.pop(f"{prefix}/bias"))


def load_flax_mha(mha: MHA, weights: dict[str, Tensor], prefix: str) -> None:
    mha.q_proj.weight.copy_(weights.pop(f"{prefix}/query/kernel").flatten(1).T)
    mha.q_proj.bias.copy_(weights.pop(f"{prefix}/query/bias").flatten())
    mha.k_proj.weight.copy_(weights.pop(f"{prefix}/key/kernel").flatten(1).T)
    mha.k_proj.bias.copy_(weights.pop(f"{prefix}/key/bias").flatten())
    mha.v_proj.weight.copy_(weights.pop(f"{prefix}/value/kernel").flatten(1).T)
    mha.v_proj.bias.copy_(weights.pop(f"{prefix}/value/bias").flatten())
    mha.out_proj.weight.copy_(weights.pop(f"{prefix}/out/kernel").flatten(0, 1).T)
    mha.out_proj.bias.copy_(weights.pop(f"{prefix}/out/bias").flatten())
