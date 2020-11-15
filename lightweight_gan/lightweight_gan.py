import torch
from math import log2
import torch.nn.functional as F
from torch import nn, einsum
from einops import rearrange

# helpers

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

# classes

class SLE(nn.Module):
    def __init__(
        self,
        *,
        chan_in,
        chan_out
    ):
        super().__init__()
        self.net = nn.Sequential(
            nn.AdaptiveAvgPool2d((4, 4)),
            nn.Conv2d(chan_in, chan_in, 4),
            nn.LeakyReLU(0.1),
            nn.Conv2d(chan_in, chan_out, 1),
            nn.Sigmoid()
        )
    def forward(self, x):
        return self.net(x)

class Generator(nn.Module):
    def __init__(
        self,
        *,
        image_size,
        latent_dim = 256,
        fmap_max = 512,
        fmap_inverse_coef = 12
    ):
        super().__init__()
        resolution = log2(image_size)
        assert resolution.is_integer(), 'image size must be a power of 2'
        fmap_max = default(fmap_max, latent_dim)

        self.initial_conv = nn.Sequential(
            nn.ConvTranspose2d(latent_dim, latent_dim * 2, 4),
            nn.BatchNorm2d(latent_dim * 2),
            nn.GLU(dim = 1)
        )

        num_layers = int(resolution) - 2
        features = list(map(lambda n: (n,  2 ** (fmap_inverse_coef - n)), range(2, num_layers + 2)))
        features = list(map(lambda n: (n[0], min(n[1], fmap_max)), features))
        features = list(map(lambda n: 3 if n[0] >= 8 else n[1], features))
        features = [latent_dim, *features]

        in_out_features = list(zip(features[:-1], features[1:]))

        self.res_layers = range(2, num_layers + 2)
        self.layers = nn.ModuleList([])
        self.res_to_feature_map = dict(zip(self.res_layers, in_out_features))

        self.sle_map = ((3, 7), (4, 8), (5, 9), (6, 10))
        self.sle_map = list(filter(lambda t: t[0] in self.res_layers and t[1] in self.res_layers, self.sle_map))
        self.sle_map = dict(self.sle_map)

        for (resolution, (chan_in, chan_out)) in zip(self.res_layers, in_out_features):
            sle = None
            if resolution in self.sle_map:
                residual_layer = self.sle_map[resolution]
                sle_chan_out = self.res_to_feature_map[residual_layer][-1]

                sle = SLE(
                    chan_in = chan_out,
                    chan_out = sle_chan_out
                )

            layer = nn.ModuleList([
                nn.Sequential(
                    nn.Upsample(scale_factor = 2),
                    nn.Conv2d(chan_in, chan_out * 2, 3, padding = 1),
                    nn.BatchNorm2d(chan_out * 2),
                    nn.GLU(dim = 1)
                ),
                sle
            ])
            self.layers.append(layer)

        self.out_conv = nn.Conv2d(features[-1], 3, 3, padding = 1)

    def forward(self, x):
        x = rearrange(x, 'b c -> b c () ()')
        x = self.initial_conv(x)

        residuals = dict()

        for (res, (up, sle)) in zip(self.res_layers, self.layers):
            x = up(x)
            if exists(sle):
                out_res = self.sle_map[res]
                residual = sle(x)
                residuals[out_res] = residual

            if res in residuals:
                x = x + residuals[res]

        x = self.out_conv(x)
        return x.tanh()

class SimpleDecoder(nn.Module):
    def __init__(
        self,
        *,
        chan_in,
        num_upsamples = 4
    ):
        super().__init__()
        self.layers = nn.ModuleList([])

        chans = chan_in
        for ind in range(num_upsamples):
            last_layer = ind == (num_upsamples - 1)
            chan_out = chans if not last_layer else 3 * 2
            layer = nn.Sequential(
                nn.Upsample(scale_factor = 2),
                nn.Conv2d(chans, chan_out, 3, padding = 1),
                nn.BatchNorm2d(chan_out),
                nn.GLU(dim = 1)
            )
            self.layers.append(layer)
            chans //= 2

    def forward(self, x):
        for layer in self.layers:
            x = layer(x)
        return x

class Discriminator(nn.Module):
    def __init__(
        self,
        *,
        image_size,
        fmap_max = 512,
        fmap_inverse_coef = 12
    ):
        super().__init__()
        resolution = log2(image_size)
        assert resolution.is_integer(), 'image size must be a power of 2'

        num_non_residual_layers = max(0, int(resolution) - 8)
        num_residual_layers = 8 - 3

        features = list(map(lambda n: (n,  2 ** (fmap_inverse_coef - n)), range(8, 2, -1)))
        features = list(map(lambda n: (n[0], min(n[1], fmap_max)), features))
        chan_in_out = zip(features[:-1], features[1:])

        self.non_residual_layers = nn.ModuleList([])
        for ind in range(num_non_residual_layers):
            first_layer = ind == 0
            last_layer = ind == (num_non_residual_layers - 1)
            chan_out = features[0][-1] if last_layer else 3

            self.non_residual_layers.append(nn.Sequential(
                nn.Conv2d(3, chan_out, 4, stride = 2, padding = 1),
                nn.BatchNorm2d(3) if not first_layer else nn.Identity(),
                nn.LeakyReLU(0.1)
            ))

        self.residual_layers = nn.ModuleList([])
        for (_, chan_in), (_, chan_out) in chan_in_out:
            self.residual_layers.append(nn.ModuleList([
                nn.Sequential(
                    nn.Conv2d(chan_in, chan_out, 4, stride = 2, padding = 1),
                    nn.BatchNorm2d(chan_out),
                    nn.LeakyReLU(0.1),
                    nn.Conv2d(chan_out, chan_out, 3, padding = 1),
                    nn.BatchNorm2d(chan_out),
                    nn.LeakyReLU(0.1)
                ),
                nn.Sequential(
                    nn.AvgPool2d(2),
                    nn.Conv2d(chan_in, chan_out, 1),
                    nn.BatchNorm2d(chan_out),
                    nn.LeakyReLU(0.1)
                ),
            ]))

        last_chan = features[-1][-1]
        self.to_logits = nn.Sequential(
            nn.Conv2d(last_chan, last_chan, 1),
            nn.BatchNorm2d(last_chan),
            nn.LeakyReLU(0.1),
            nn.Conv2d(last_chan, 1, 4)
        )

        self.decoder = SimpleDecoder(chan_in = last_chan)

    def forward(self, x):
        orig_img = x

        for layer in self.non_residual_layers:
            x = layer(x)

        for (layer, residual_layer) in self.residual_layers:
            x = layer(x) + residual_layer(x)

        out = self.to_logits(x)

        # self-supervised auto-encoding loss

        reconstructed_img = self.decoder(x)

        aux_loss = F.mse_loss(
            reconstructed_img,
            F.interpolate(orig_img, size = reconstructed_img.shape[2:])
        )

        return out.flatten(1), aux_loss

class LightweightGAN(nn.Module):
    def __init__(self):
        super().__init__()
    def forward(self, x):
        return x