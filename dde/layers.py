#!/usr/bin/env python
# -*- coding:utf-8 -*-

import numpy as np
import keras.backend as K
from keras import activations, initializations
from keras.engine.topology import Layer
from keras.layers import merge
import theano
import theano.tensor as T



class MoleculeConv(Layer):
    def __init__(self, units, inner_dim, depth=2, init_output='uniform',
                 activation_output='softmax', init_inner='identity',
                 activation_inner='linear', scale_output=0.01, padding=False, 
                 dropout_rate_outer=0.0, dropout_rate_inner=0.0,
                 padding_final_size=None, atomic_fp=False, **kwargs):
        if depth < 1:
            quit('Cannot use MoleculeConv with depth zero')
        self.init_output = initializations.get(init_output)
        self.activation_output = activations.get(activation_output)
        self.init_inner = initializations.get(init_inner)
        self.activation_inner = activations.get(activation_inner)
        self.units = units
        self.inner_dim = inner_dim
        self.depth = depth
        self.scale_output = scale_output
        self.padding = padding
        self.padding_final_size = padding_final_size
        self.dropout_rate_outer = dropout_rate_outer
        self.dropout_rate_inner = dropout_rate_inner
        self.atomic_fp = atomic_fp
        self.mask_inner = []
        self.mask_output = []
        self.masks_inner_vals = []
        self.masks_output_vals = []

        self.initial_weights = None
        self.input_dim = 4  # each entry is a 3D N_atom x N_atom x N_feature tensor

        super(MoleculeConv, self).__init__(**kwargs)

    def gen_masks(self, rngs):
        for rng in rngs:
            retain_prob = 1.0 - self.dropout_rate_inner
            vals = []
            for mask in self.mask_inner:
                size = K.int_shape(mask)
                vals.append(rng.binomial(n=1,p=retain_prob,size=size).astype(np.float32))
            self.masks_inner_vals.append(vals)

            retain_prob = 1.0 - self.dropout_rate_outer
            vals = []
            for mask in self.mask_output:
                size = K.int_shape(mask)
                vals.append(rng.binomial(n=1,p=retain_prob,size=size).astype(np.float32))
            self.masks_output_vals.append(vals)

    def set_mask(self, idx):
        for mask, vals in zip(self.mask_inner, self.masks_inner_vals[idx]):
            K.set_value(mask, vals)
        for mask, vals in zip(self.mask_output, self.masks_output_vals[idx]):
            K.set_value(mask, vals)

    def build(self, input_shape):
        """
        Build internal weights and parameter attribute.
        """

        # Define template weights for inner FxF
        W_inner = self.init_inner((self.inner_dim, self.inner_dim))
        b_inner = K.zeros((1, self.inner_dim))
        # Initialize weights tensor
        self.W_inner = K.variable(T.tile(W_inner, (self.depth + 1, 1, 1)).eval())
        self.W_inner.name = 'T:W_inner'
        self.b_inner = K.variable(T.tile(b_inner, (self.depth + 1, 1, 1)).eval())
        self.b_inner.name = 'T:b_inner'

        # Define template weights for output FxL
        W_output = self.init_output((self.inner_dim, self.units))
        b_output = K.zeros((1, self.units))
        # Initialize weights tensor
        self.W_output = K.variable(T.tile(W_output, (self.depth + 1, 1, 1)).eval())
        self.W_output.name = 'T:W_output'
        self.b_output = K.variable(T.tile(b_output, (self.depth + 1, 1, 1)).eval())
        self.b_output.name = 'T:b_output'

        # Pack params
        self.trainable_weights = [self.W_inner,
                                  self.b_inner,
                                  self.W_output,
                                  self.b_output]

    def get_output_shape_for(self, input_shape):
        if self.atomic_fp:
            return input_shape[0], input_shape[1], self.units
        else:
            return input_shape[0], self.units

    def padding_tensor(self, fp_all_depth):
        padding_result = T.zeros((self.padding_final_size, self.units))    
        num_non_H_atom = T.shape(fp_all_depth)[0]
        padding_result = T.set_subtensor(padding_result[:num_non_H_atom], fp_all_depth)
        return padding_result

    def call(self, x, mask=None):
        if self.atomic_fp:
            (output, updates) = theano.scan(lambda x_one: self.padding_tensor(self.get_output_singlesample(x_one)), sequences=x)
        else:
            (output, updates) = theano.scan(lambda x_one: self.get_output_singlesample(x_one), sequences=x)
        return output

    def get_output_singlesample(self, M):
        """
        Given a molecule tensor M, calculate its fingerprint.
        """
        # if incoming tensor M has padding
        # remove padding first
        # this is the part getting slow-down
        if self.padding:
            rowsum = M.sum(axis=0)
            trim = rowsum[:, -1]
            trim_to = T.eq(trim, 0).nonzero()[0][0]  # first index with no bonds
            M = M[:trim_to, :trim_to, :]  # reduced graph

        # dimshuffle to get diagonal items to
        # form atom matrix A
        (A_tmp, updates) = theano.scan(lambda x: x.diagonal(), sequences=M[:,:,:-1].dimshuffle((2, 0, 1)))
        # Now the attributes is (N_features x N_atom), so we need to transpose
        A = A_tmp.T

        # get connectivity matrix: N_atom * N_atom
        C = M[:, :, -1] + T.identity_like(M[:, :, -1])

        # get bond tensor: N_atom * N_atom * (N_features-1)
        B_tmp = M[:, :, :-1] - A
        coeff = K.concatenate([M[:, :, -1:]]*self.inner_dim, axis=2)
        B = merge([B_tmp, coeff], mode="mul")

        # Get initial fingerprint
        presum_fp = self.attributes_to_fp_contribution(A, 0)
        fp_all_depth = presum_fp

        # Iterate through different depths, updating atom matrix each time
        A_new = A
        for depth in range(self.depth):
            temp = K.dot(K.dot(C, A_new) + K.sum(B, axis=1), self.W_inner[depth+1, :, :])\
                + self.b_inner[depth+1, 0, :]
                
            if self.dropout_rate_inner != 0.0:
                mask = K.variable(np.ones(shape=(self.padding_final_size, self.inner_dim),dtype=np.float32))
                self.mask_inner.append(mask)
                n_atom = K.shape(temp)[0]
                temp *= mask[:n_atom,:]
                
            A_new = self.activation_inner(temp)

            presum_fp_new = self.attributes_to_fp_contribution(A_new, depth + 1)
            fp_all_depth = fp_all_depth + presum_fp_new
        
        if self.atomic_fp:
            fp = fp_all_depth
        else:
            fp = K.sum(fp_all_depth, axis=0)  # sum across atom contributions

        return fp

    def attributes_to_fp_contribution(self, attributes, depth):
        """
        Given a 2D tensor of attributes where the first dimension corresponds to a single
        node, this method will apply the output sparsifying (often softmax) function and return
        the contribution to the fingerprint.
        """
        # Apply output activation function
        output_dot = K.dot(attributes, self.W_output[depth, :, :])
        output_dot.name = 'output_dot'
        output_bias = self.b_output[depth, 0, :]
        output_bias.name = 'output_bias'
        temp = output_dot + output_bias
        
        if self.dropout_rate_outer != 0.0:
            mask = K.variable(np.ones(shape=(self.padding_final_size,self.units),dtype=np.float32))
            self.mask_output.append(mask)
            n_atom = K.shape(temp)[0]
            temp *= mask[:n_atom,:]
        
        output_activated = self.activation_output(temp)
        output_activated.name = 'output_activated'
        return output_activated

    def get_config(self):
        config = {'units': self.units,
                  'inner_dim': self.inner_dim,
                  'init_output': self.init_output.__name__,
                  'init_inner': self.init_inner.__name__,
                  'activation_inner': self.activation_inner.__name__,
                  'activation_output': self.activation_output.__name__,
                  'scale_output': self.scale_output,
                  'padding': self.padding,
                  'padding_final_size': self.padding_final_size,
                  'depth' : self.depth,
                  'dropout_rate_inner': self.dropout_rate_inner,
                  'dropout_rate_outer': self.dropout_rate_outer,
                  'atomic_fp': self.atomic_fp}
        base_config = super(MoleculeConv, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))
