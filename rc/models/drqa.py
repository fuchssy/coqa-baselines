import torch
import torch.nn as nn
import src.layers as layers
import torch.nn.functional as F


class DrQA(nn.Module):
    """Network for the Document Reader module of DrQA."""
    _RNN_TYPES = {'lstm': nn.LSTM, 'gru': nn.GRU, 'rnn': nn.RNN}

    def __init__(self, config, w_embedding, c_embedding=None):
        """Configuration, word embeddings, (optional) character embeddings."""
        super(DrQA, self).__init__()
        # Store config
        self.config = config
        self.w_embedding = w_embedding
        input_w_dim = self.w_embedding.embedding_dim + config["char_embed"]  # "char_embed" is 0 if c_embedding is None.
        q_input_size = input_w_dim
        if self.config['fix_embeddings']:
            for p in self.w_embedding.parameters():
                p.requires_grad = False

        if config["char_embed"]:
            self.char_layer = layers.CharEmbeddingLayer(
                char_embedding=c_embedding,
                input_size=config["char_embed"],
                hidden_size=config["char_embed"],
                layer_type=config["char_layer"],
                dropout=config["dropout_char"],
                variational_dropout=config["variational_dropout"],
                filter_height=config["filter_height"],
            )

        # Projection for attention weighted question
        if self.config['use_qemb']:
            self.qemb_match = layers.SeqAttnMatch(input_w_dim)

        # Input size to RNN: word emb + question emb + manual features
        doc_input_size = input_w_dim + self.config['num_features']
        if self.config['use_qemb']:
            doc_input_size += input_w_dim

        # Project document and question to the same size as their encoders
        if self.config['resize_rnn_input']:
            self.doc_linear = nn.Linear(doc_input_size, config['hidden_size'], bias=True)
            self.q_linear = nn.Linear(input_w_dim, config['hidden_size'], bias=True)
            doc_input_size = q_input_size = config['hidden_size']

        # RNN document encoder
        self.doc_rnn = layers.StackedBRNN(
            input_size=doc_input_size,
            hidden_size=config['hidden_size'],
            num_layers=config['num_layers'],
            dropout_rate=config['dropout_rnn'],
            dropout_output=config['dropout_rnn_output'],
            variational_dropout=config['variational_dropout'],
            concat_layers=config['concat_rnn_layers'],
            rnn_type=self._RNN_TYPES[config['rnn_type']],
            padding=config['rnn_padding'],
            bidirectional=True,
        )

        # RNN question encoder
        self.question_rnn = layers.StackedBRNN(
            input_size=q_input_size,
            hidden_size=config['hidden_size'],
            num_layers=config['num_layers'],
            dropout_rate=config['dropout_rnn'],
            dropout_output=config['dropout_rnn_output'],
            variational_dropout=config['variational_dropout'],
            concat_layers=config['concat_rnn_layers'],
            rnn_type=self._RNN_TYPES[config['rnn_type']],
            padding=config['rnn_padding'],
            bidirectional=True,
        )

        # Output sizes of rnn encoders
        doc_hidden_size = 2 * config['hidden_size']
        question_hidden_size = 2 * config['hidden_size']
        if config['concat_rnn_layers']:
            doc_hidden_size *= config['num_layers']
            question_hidden_size *= config['num_layers']

        if config['doc_self_attn']:
            self.doc_self_attn = layers.SeqAttnMatch(doc_hidden_size)
            doc_hidden_size = doc_hidden_size + question_hidden_size

        # Question merging
        if config['question_merge'] not in ['avg', 'self_attn']:
            raise NotImplementedError('question_merge = %s' % config['question_merge'])
        if config['question_merge'] == 'self_attn':
            self.self_attn = layers.LinearSeqAttn(question_hidden_size)

        # Bilinear attention for span start/end
        self.start_attn = layers.BilinearSeqAttn(
            doc_hidden_size,
            question_hidden_size,
        )
        q_rep_size = question_hidden_size + doc_hidden_size if config['span_dependency'] else question_hidden_size
        self.end_attn = layers.BilinearSeqAttn(
            doc_hidden_size,
            q_rep_size,
        )

    def forward(self, ex):
        """Inputs:
        xq = question word indices             (batch, max_q_len)
        xq_mask = question padding mask        (batch, max_q_len)
        xd = document word indices             (batch, max_d_len)
        xd_f = document word features indices  (batch, max_d_len, nfeat)
        xd_mask = document padding mask        (batch, max_d_len)
        targets = span targets                 (batch,)
        chunk_targets = chunk targets          (batch,)

        Optional:
        c_emb = character indices unique words (t_unique_words, max_w_len)
        c_emb_mask = character indices mask    (t_unique_words, max_w_len)
        c_layer_lookup = lookup table          (t_unique_words + 1, char_embed)
        xqc = question word lookup indices     (batch, max_q_len)
        xdc = document word lookup indices     (batch, max_c_len)
        """

        # Embed both document and question
        xq_emb = self.w_embedding(ex['xq'])                         # (batch, max_q_len, word_embed)
        xd_emb = self.w_embedding(ex['xd'])                         # (batch, max_d_len, word_embed)

        shared_axes = [2] if self.config['word_dropout'] else []
        xq_emb = layers.dropout(xq_emb, self.config['dropout_emb'], shared_axes=shared_axes, training=self.training)
        xd_emb = layers.dropout(xd_emb, self.config['dropout_emb'], shared_axes=shared_axes, training=self.training)
        xd_mask = ex['xd_mask']
        xq_mask = ex['xq_mask']

        # Character embeddings
        if self.config["char_embed"] > 0:
            xc_rep = self.char_layer(ex['c_emb'], ex['c_emb_mask'])
            # Copy weights into lookup nn.embedding (leave index-0 padding-index)
            ex['c_layer_lookup'].weight[1:, :] = xc_rep
            xqc_emb = ex['c_layer_lookup'](ex['xqc'])               # (batch, max_q_len, char_emb)
            xdc_emb = ex['c_layer_lookup'](ex['xdc'])               # (batch, max_d_len, char_emb)
            xq_emb = torch.cat([xq_emb, xqc_emb], 2)                # (batch, max_q_len, word_embed + char_emb)
            xd_emb = torch.cat([xd_emb, xdc_emb], 2)                # (batch, max_d_len, word_embed + char_emb)

        # Add attention-weighted question representation
        if self.config['use_qemb']:
            xq_weighted_emb = self.qemb_match(xd_emb, xq_emb, xq_mask)
            drnn_input = torch.cat([xd_emb, xq_weighted_emb], 2)
        else:
            drnn_input = xd_emb

        if self.config["num_features"] > 0:
            drnn_input = torch.cat([drnn_input, ex['xd_f']], 2)

        # Project document and question to the same size as their encoders
        if self.config['resize_rnn_input']:
            drnn_input = F.relu(self.doc_linear(drnn_input))
            xq_emb = F.relu(self.q_linear(xq_emb))
            if self.config['dropout_ff'] > 0:
                drnn_input = F.dropout(drnn_input, training=self.training)
                xq_emb = F.dropout(xq_emb, training=self.training)

        # Encode document with RNN
        doc_hiddens = self.doc_rnn(drnn_input, xd_mask)       # (batch, max_d_len, hidden_size)

        # Document self attention
        if self.config['doc_self_attn']:
            xd_weighted_emb = self.doc_self_attn(doc_hiddens, doc_hiddens, xd_mask)
            doc_hiddens = torch.cat([doc_hiddens, xd_weighted_emb], 2)

        # Encode question with RNN + merge hiddens
        question_hiddens = self.question_rnn(xq_emb, xq_mask)
        if self.config['question_merge'] == 'avg':
            q_merge_weights = layers.uniform_weights(question_hiddens, xq_mask)
        elif self.config['question_merge'] == 'self_attn':
            q_merge_weights = self.self_attn(question_hiddens.contiguous(), xq_mask)
        question_hidden = layers.weighted_avg(question_hiddens, q_merge_weights)

        # Predict start and end positions
        start_scores = self.start_attn(doc_hiddens, question_hidden, xd_mask)
        if self.config['span_dependency']:
            question_hidden = torch.cat([question_hidden, (doc_hiddens * start_scores.exp().unsqueeze(2)).sum(1)], 1)
        end_scores = self.end_attn(doc_hiddens, question_hidden, xd_mask)

        chunk_acc = (ex['chunk_targets'] >= 0).float().sum() / len(ex['chunk_targets'])

        return {'score_s': start_scores,
                'score_e': end_scores,
                'targets': ex['targets'],
                'chunk_target_acc': chunk_acc,
                'chunk_any_acc': chunk_acc}