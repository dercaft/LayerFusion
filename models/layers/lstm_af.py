from torch import nn
import torch
from models.networks.generative.flows.flow import Flow as MADE_flow
from models.layers.ff import FeedForwardNet
from util.batchers import reverse_padded_sequence


class LSTM_AFLayer(nn.Module):
    def __init__(self, layer_num, inp_dim, n_hidden_layers, n_hidden_units, dropout_p, transform_function,
                 rnn_cond_dim=None,
                 swap_trngen_dirs=False, reverse_inps=False, hiddenflow_params={}, dlocs=['recurrent', 'rnn_outp'],
                 notimecontext=False):
        super().__init__()

        self.rnn_inp_drop = nn.Dropout(dropout_p)
        self.rnn_outp_drop = nn.Dropout(dropout_p)

        lstm_inp_dim = inp_dim
        self.use_rnn_cond_inp = rnn_cond_dim is not None
        if self.use_rnn_cond_inp:
            lstm_inp_dim += rnn_cond_dim
            if not notimecontext:
                self.initial_hidden_cond_ff = FeedForwardNet(rnn_cond_dim, n_hidden_units,
                                                             2 * n_hidden_units * n_hidden_layers, 1, 'relu')

        if not notimecontext:
            self.lstm = nn.LSTM(lstm_inp_dim, n_hidden_units, n_hidden_layers,
                                dropout=dropout_p if 'recurrent' in dlocs else 0)
            after_rnn_inp_units = n_hidden_units
        else:
            after_rnn_inp_units = rnn_cond_dim

        # Whether or not a MADE autoregressive flow (hiddenflow) should be used to model p(z_t),
        # or if the elements of z_t should be independent
        self.use_hiddenflow = not hiddenflow_params['nohiddenflow']
        if self.use_hiddenflow:
            if reverse_inps:
                raise NotImplementedError(
                    'hiddenflow with reversing the inputs in time has not been implemented. Will have to take into '
                    'account the fact that ' +
                    'IAF and AF flows both use forward for the training pass, which is diffent than the convention here.')

            hiddenflow_layers = hiddenflow_params['hiddenflow_layers']
            hiddenflow_units = hiddenflow_params['hiddenflow_units']
            hiddenflow_flow_layers = hiddenflow_params[
                'hiddenflow_flow_layers']  # if > 1, automatically reverses the order
            hiddenflow_scf_layers = hiddenflow_params['hiddenflow_scf_layers']
            hiddenflow_reverse_first = layer_num % 2 == 1

            if hiddenflow_units <= inp_dim:
                raise ValueError(
                    'Error, hiddenflow_units must be greater than the inp_dim so all inp variables have connections to the output')

            MADE_dropout = [dropout_p, dropout_p] if 'ff' in dlocs else [0, 0]
            self.outp_net = MADE_flow(inp_dim, hiddenflow_layers, hiddenflow_units, 'relu', hiddenflow_flow_layers,
                                      transform_function, iaf_like=False,
                                      swap_trngen_dirs=swap_trngen_dirs, conditional_inp_dim=after_rnn_inp_units,
                                      dropout=MADE_dropout,
                                      reverse_between_layers=True, scf_layers=hiddenflow_scf_layers,
                                      reverse_first_layer=hiddenflow_reverse_first)
        else:
            if notimecontext:
                raise ValueError('notimecontext does not make sense without MADE layers')

            self.outp_net = nn.Linear(after_rnn_inp_units, transform_function.num_params * inp_dim)

            self.num_params = transform_function.num_params
            self.train_func = transform_function.standard if swap_trngen_dirs else transform_function.reverse
            self.gen_func = transform_function.reverse if swap_trngen_dirs else transform_function.standard

        self.layer_num = layer_num  # Needed to keep track of hidden states
        self.n_hidden_layers = n_hidden_layers  # Needed for init_hidden
        self.n_hidden_units = n_hidden_units  # Needed for init_hidden
        self.inp_dim = inp_dim  # Needed for init_last_nn_outp
        self.reverse_inps = reverse_inps
        self.dlocs = dlocs  # Options are [rnn_inp, recurrent, rnn_outp, made]
        self.notimecontext = notimecontext

        self.reset_parameters()

    def reset_parameters(self):
        init_range = 0.07
        if not self.use_hiddenflow:
            self.outp_net.weight.data.uniform_(-init_range, init_range)
            self.outp_net.bias.data.zero_()

    def forward(self, inputs):
        """
            Defines the reverse pass which is used during training
            logdet means log det del_y/del_x
        """

        y, logdet, hiddens, rnn_cond_inp, lengths = inputs  # y is [T, B, inp_dim]

        y_packed = y
        cur_rnn_cond_inp = rnn_cond_inp
        B = y.shape[1]

        lengths_inp = lengths
        if not self.notimecontext:
            if self.reverse_inps:
                y_packed = reverse_padded_sequence(y_packed, lengths_inp)
                cur_rnn_cond_inp = reverse_padded_sequence(rnn_cond_inp, lengths_inp)

            if self.use_rnn_cond_inp:
                actual_hidden = self.initial_hidden_cond_ff(cur_rnn_cond_inp[0]).view(B, self.n_hidden_layers,
                                                                                      self.n_hidden_units,
                                                                                      2)  # [B, layers, hidden, 2]
                actual_hidden = actual_hidden.transpose(0, 1)  # [layers, B, hidden, 2]
                actual_hidden = tuple([actual_hidden[..., 0].contiguous(), actual_hidden[..., 1].contiguous()])
                hiddens[self.layer_num] = actual_hidden

                cur_rnn_cond_inp_shifted = torch.cat(
                    (cur_rnn_cond_inp[1:], cur_rnn_cond_inp.new_zeros((1, *cur_rnn_cond_inp.shape[1:]))), 0)
                y_packed = torch.cat((y_packed, cur_rnn_cond_inp_shifted), -1)

        if 'rnn_inp' in self.dlocs:
            y_packed = self.rnn_inp_drop(y_packed)

        if not self.notimecontext:
            total_length = y_packed.shape[0]
            y_packed = nn.utils.rnn.pack_padded_sequence(y_packed, lengths_inp)
            rnn_outp, final_hidden = self.lstm(y_packed, hiddens[self.layer_num])
            rnn_outp = nn.utils.rnn.pad_packed_sequence(rnn_outp, total_length=total_length)[0]
            rnn_outp = torch.cat((hiddens[self.layer_num][0][-1:], rnn_outp), 0)[
                       :-1]  # This will correctly shift the outputs so they are actually autoregressive

            hiddens[self.layer_num] = final_hidden

            if self.reverse_inps:  # Undo the reverse ordering so the outputs have the correct ordering
                rnn_outp = reverse_padded_sequence(rnn_outp, lengths_inp)

            if 'rnn_outp' in self.dlocs:
                rnn_outp = self.rnn_outp_drop(rnn_outp)

        if self.use_hiddenflow:
            hiddenflow_conditional = cur_rnn_cond_inp if self.notimecontext else rnn_outp
            x_new, change_logdet = self.outp_net([y, hiddenflow_conditional])
        else:
            nn_outp = self.outp_net(rnn_outp)
            nn_outp = nn_outp.view(*nn_outp.shape[:-1], self.inp_dim, self.num_params)

            x_new, change_logdet = self.train_func(y, nn_outp)  # x is [T, B, inp_dim], change_logdet is [T, B]

        x = x_new
        logdet += change_logdet

        return x, logdet, hiddens, rnn_cond_inp, lengths

    def generate(self, inputs):
        """
            Defines the forward pass which is used during testing
            logdet means log det del_y/del_x
        """

        x, logdet, hiddens, rnn_cond_inp, lengths = inputs

        rnn_cond_inp_touse = rnn_cond_inp
        if self.reverse_inps:
            x = reverse_padded_sequence(x, lengths)
            rnn_cond_inp_touse = reverse_padded_sequence(rnn_cond_inp, lengths)

        rnn_cond_inp_touse = torch.cat(
            (rnn_cond_inp_touse, rnn_cond_inp_touse.new_zeros((1, *rnn_cond_inp_touse.shape[1:]))), 0)

        y = torch.tensor(x)  # [T, B, inp_dim]
        change_logdet = torch.zeros_like(logdet)  # [T, B]

        if self.use_rnn_cond_inp:
            B = x.shape[1]
            actual_hidden = self.initial_hidden_cond_ff(rnn_cond_inp_touse[0]).view(B, self.n_hidden_layers,
                                                                                    self.n_hidden_units,
                                                                                    2)  # [B, layers, hidden, 2]
            actual_hidden = actual_hidden.transpose(0, 1)  # [layers, B, hidden, 2]
            actual_hidden = tuple([actual_hidden[..., 0].contiguous(), actual_hidden[..., 1].contiguous()])
            hiddens[self.layer_num] = actual_hidden

        last_rnn_outp = hiddens[self.layer_num][0][-1:]  # [1, B, hidden]
        last_hiddens = hiddens[self.layer_num]
        for t in range(x.shape[0]):
            if 'rnn_outp' in self.dlocs:
                last_rnn_outp = self.rnn_outp_drop(last_rnn_outp)

            if self.use_hiddenflow:
                y[t:t + 1], new_partial_logdet = self.outp_net.generate([x[t], last_rnn_outp[0]])
            else:
                nn_outp = self.outp_net(last_rnn_outp)
                nn_outp = nn_outp.view(1, last_rnn_outp.shape[1], self.inp_dim, self.num_params)

                y[t:t + 1], new_partial_logdet = self.gen_func(x[t:t + 1], nn_outp)

            change_logdet[t] = new_partial_logdet

            if self.use_rnn_cond_inp:
                rnn_cond_inp_t = rnn_cond_inp_touse[t + 1:t + 2]
                lstm_inp = torch.cat((y[t:t + 1], rnn_cond_inp_t), -1)
            else:
                lstm_inp = y[t:t + 1].clone()

            if 'rnn_inp' in self.dlocs:
                lstm_inp = self.rnn_inp_drop(lstm_inp)

            last_rnn_outp, last_hiddens = self.lstm(lstm_inp, last_hiddens)

        for h in last_hiddens:
            h[:, :, :] = -9999999999  # If lengths is provided, then the hidden output provided by this function is wrong.
                                      # If they're ever used for anything, this should make it clear there's an error

        hiddens[self.layer_num] = last_hiddens

        if self.reverse_inps:
            y = reverse_padded_sequence(y, lengths)
            change_logdet = reverse_padded_sequence(change_logdet, lengths)

        return y, logdet + change_logdet, hiddens, rnn_cond_inp, lengths

    def init_hidden(self, batch_size):
        weight = next(self.parameters())
        h = weight.new_zeros(self.n_hidden_layers, batch_size, self.n_hidden_units)
        c = weight.new_zeros(self.n_hidden_layers, batch_size, self.n_hidden_units)
        return (h, c)
