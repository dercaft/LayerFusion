import torch
import torch.nn as nn
from torch.nn.parameter import Parameter
import numpy as np
import torch.nn.functional as F


class MaskedConv2d(nn.Module):
    """
    Creates masked convolutional autoregressive layer for pixelCNN.
    For input (x) to hidden (h) or hidden to hidden layers choose diagonal_zeros = False.
    For hidden to output (y) layers:
    If output depends on input through y_i = f(x_{<i}) set diagonal_zeros = True.
    Else if output depends on input through y_i = f(x_{<=i}) set diagonal_zeros = False.
    """

    def __init__(self, in_features, out_features, size_kernel=(3, 3), diagonal_zeros=False, bias=True):
        super(MaskedConv2d, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.size_kernel = size_kernel
        self.diagonal_zeros = diagonal_zeros
        self.weight = Parameter(torch.FloatTensor(out_features, in_features, *self.size_kernel))
        if bias:
            self.bias = Parameter(torch.FloatTensor(out_features))
        else:
            self.register_parameter('bias', None)
        mask = torch.from_numpy(self.build_mask())
        if torch.cuda.is_available():
            mask = mask.cuda()
        self.mask = torch.autograd.Variable(mask, requires_grad=False)
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.kaiming_normal(self.weight)
        if self.bias is not None:
            self.bias.data.zero_()

    def build_mask(self):
        n_in, n_out = self.in_features, self.out_features

        assert n_out % n_in == 0 or n_in % n_out == 0, "%d - %d" % (n_in, n_out)

        # Build autoregressive mask
        l = (self.size_kernel[0] - 1) // 2
        m = (self.size_kernel[1] - 1) // 2
        mask = np.ones((n_out, n_in, self.size_kernel[0], self.size_kernel[1]), dtype=np.float32)
        mask[:, :, :l, :] = 0
        mask[:, :, l, :m] = 0

        if n_out >= n_in:
            k = n_out // n_in
            for i in range(n_in):
                mask[i * k:(i + 1) * k, i + 1:, l, m] = 0
                if self.diagonal_zeros:
                    mask[i * k:(i + 1) * k, i:i + 1, l, m] = 0
        else:
            k = n_in // n_out
            for i in range(n_out):
                mask[i:i + 1, (i + 1) * k:, l, m] = 0
                if self.diagonal_zeros:
                    mask[i:i + 1, i * k:(i + 1) * k:, l, m] = 0

        return mask

    def forward(self, x):
        output = F.conv2d(x, self.mask * self.weight, bias=self.bias, padding=(1, 1))
        return output

    def __repr__(self):
        if self.bias is not None:
            bias = True
        else:
            bias = False
        return self.__class__.__name__ + ' (' \
            + str(self.in_features) + ' -> ' \
            + str(self.out_features) + ', diagonal_zeros=' \
            + str(self.diagonal_zeros) + ', bias=' \
            + str(bias) + ', size_kernel=' \
            + str(self.size_kernel) + ')'
