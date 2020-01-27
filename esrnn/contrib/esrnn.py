import logging
import os
import yaml
import time
import numpy as np
import dynet_config

dynet_config.set(random_seed=120652)
import dynet as dy
import pandas as pd

from pathlib import Path


# TODO: validation/test periods
# TODO: when we filter ids length wont match (zeros at the end)

def dy_arrInput(np_arr):
    """
    np_arr: numpy array
    return: array expression for dynet
    """
    dim = np_arr.shape[0]
    arr_ex = dy.vecInput(dim)
    arr_ex.set(np_arr)
    return arr_ex


# ORAX: M4 CATEGORIES ERASED
class M4TS():
    def __init__(self, mc, y, ds, id):
        self.id = id
        n = len(y)
        if mc.lback > 0:
            if (n - mc.lback * mc.output_size_i > 0):
                first = n - mc.lback * mc.output_size_i
                pastLast = n - (mc.lback - 1) * mc.output_size_i
                self.y = y[:first]
                self.y_test = y[first:pastLast]
        else:
            self.y = y
        if (len(self.y) > mc.max_series_length):
            self.y = y[-mc.max_series_length:]

        self.last_ds = ds[-1]

        # category_dict = {'Demographic': 0, 'Finance': 1, 'Industry': 2, # ORAX
        #                 'Macro': 3, 'Micro': 4, 'Other': 5}
        # self.categories_vect = np.zeros((6,1))
        # self.categories_vect[category_dict[category]] = 1


class ModelConfig(object):
    def __init__(self, config_file, root_dir, copy=1):
        with open(config_file, 'r') as stream:
            config = yaml.safe_load(stream)

        # Train Parameters
        self.dataset_name = config['dataset_name']
        self.max_epochs = config['train_parameters']['max_epochs']
        self.freq_of_test = config['train_parameters']['freq_of_test']

        self.learning_rate = float(config['train_parameters']['learning_rate'])
        self.lr_scheduler_step_size = config['train_parameters']['lr_scheduler_step_size']
        self.per_series_lr_multip = config['train_parameters']['per_series_lr_multip']
        self.gradient_eps = float(config['train_parameters']['gradient_eps'])
        self.gradient_clipping_threshold = config['train_parameters']['gradient_clipping_threshold']
        self.noise_std = config['train_parameters']['noise_std']
        self.numeric_threshold = float(config['train_parameters']['numeric_threshold'])

        self.level_variability_penalty = config['train_parameters']['level_variability_penalty']
        self.c_state_penalty = config['train_parameters']['c_state_penalty']

        self.percentile = config['train_parameters']['percentile']
        self.training_percentile = config['train_parameters']['training_percentile']
        self.tau = self.percentile / 100.
        self.training_tau = self.training_percentile / 100.

        # Model Parameters
        self.state_hsize = config['model_parameters']['state_hsize']
        self.lback = config['model_parameters']['lback']
        self.dilations = config['model_parameters']['dilations']
        self.add_nl_layer = config['model_parameters']['add_nl_layer']
        self.attention_hsize = self.state_hsize

        # Data Parameters
        self.seasonality = config['data_parameters']['seasonality']
        self.input_size = config['data_parameters']['input_size']
        self.input_size_i = self.input_size
        self.output_size = config['data_parameters']['output_size']
        self.output_size_i = self.output_size
        self.exogenous_size = config['data_parameters']['exogenous_size']
        self.frequency = config['data_parameters']['frequency']

        self.min_inp_seq_length = config['data_parameters']['min_inp_seq_length']
        self.min_series_length = self.input_size_i + self.output_size_i + self.min_inp_seq_length + 2
        if self.seasonality == 4:
            self.max_series_length = (40 * self.seasonality) + self.min_series_length
        elif self.seasonality == 7:
            self.max_series_length = (20 * self.seasonality) + self.min_series_length
        elif self.seasonality == 12:
            self.max_series_length = (20 * self.seasonality) + self.min_series_length
        elif self.seasonality == 30:
            self.max_series_length = (20 * self.seasonality) + self.min_series_length

        self.max_num_series = config['data_parameters']['max_num_series']
        # self.data_dir = config['data_parameters']['data_dir'] # ORAX
        self.output_dir = config['data_parameters']['output_dir']

        self.root_dir = root_dir
        self.copy = copy


class ES(object):
    def __init__(self, mc):
        self.mc = mc
        self.max_num_series = self.mc.max_num_series
        self.seasonality = self.mc.seasonality
        self.output_size = self.mc.output_size

        pc = dy.ParameterCollection()

        # Smoothing and Seasonalities
        init_sms = np.ones((self.max_num_series, 1)) * 0.5
        self.lev_sms = pc.add_lookup_parameters((self.max_num_series, 1), init=init_sms)
        self.seas_sms = pc.add_lookup_parameters((self.max_num_series, 1), init=init_sms)

        init_seas = np.ones((self.max_num_series, self.seasonality)) * 0.5
        self.init_seas = pc.add_lookup_parameters((self.max_num_series, self.seasonality),
                                                  init=init_seas)
        self.pc = pc

    def declare_expr(self, ts_object):
        # Get the per series parameters
        self.lev_sms_ex = dy.logistic(dy.lookup(self.lev_sms, ts_object.id))
        self.seas_sms_ex = dy.logistic(dy.lookup(self.seas_sms, ts_object.id))
        self.init_seas_ex = dy.exp(dy.lookup(self.init_seas, ts_object.id))

    def compute_levels_seasons(self, ts_object):
        lev_sms_ex = self.lev_sms_ex
        seas_sms_ex = self.seas_sms_ex
        init_seas_ex = self.init_seas_ex

        # Initialize seasonalities, levels
        seasonalities = []
        for i in range(self.seasonality):
            seasonalities.append(init_seas_ex[i])
        seasonalities.append(init_seas_ex[0])

        levels = []
        log_diff_of_levels = []
        levels.append(dy.cdiv(dy.scalarInput(ts_object.y[0]), seasonalities[0]))

        # Calculate level and seasonalities for current time steps
        for i in range(1, len(ts_object.y)):
            newlev_ex = ts_object.y[i] * dy.cdiv(lev_sms_ex, seasonalities[i]) + \
                        (1 - lev_sms_ex) * levels[i - 1]
            newseason_ex = ts_object.y[i] * dy.cdiv(seas_sms_ex, newlev_ex) + \
                           (1 - seas_sms_ex) * seasonalities[i]
            diff_ex = dy.log(newlev_ex) - dy.log(levels[i - 1])
            levels.append(newlev_ex)
            log_diff_of_levels.append(diff_ex)
            seasonalities.append(newseason_ex)

        # Completion of seasonalities if prediction horizon is larger than seasonality
        # Naive2 like prediction, to avoid recursive forecasting 
        if self.output_size > self.seasonality:
            start_seasonality_ext = len(seasonalities) - self.seasonality
            end_seasonality_ext = start_seasonality_ext + self.output_size - self.seasonality
            seasonalities = seasonalities + seasonalities[start_seasonality_ext:end_seasonality_ext]

        return levels, seasonalities, log_diff_of_levels

    def __call__(self):
        pass


class LSTM(object):
    def __init__(self, mc):
        self.mc = mc
        input_size = mc.input_size
        state_hsize = mc.state_hsize
        output_size = mc.output_size
        dilations = mc.dilations
        exogenous_size = mc.exogenous_size
        self.layers = len(mc.dilations)

        self.pc = dy.ParameterCollection()

        self.rnn_builders = []
        builder = dy.VanillaLSTMBuilder(1, input_size + exogenous_size, state_hsize, self.pc)  # SimpleRNNBuilder
        self.rnn_builders.append(builder)

        if self.layers > 1:
            for l in range(1, self.layers):
                builder = dy.VanillaLSTMBuilder(1, state_hsize, state_hsize, self.pc)  # SimpleRNNBuilder
                self.rnn_builders.append(builder)

        if self.mc.add_nl_layer:
            self.MLPW = self.pc.add_parameters((state_hsize, state_hsize))
            self.MLPB = self.pc.add_parameters(state_hsize)

        self.adapterW = self.pc.add_parameters((output_size, state_hsize))
        self.adapterB = self.pc.add_parameters(output_size)

    def declare_expr(self):
        self.rnn_stack = []
        for l in range(self.layers):
            self.rnn_stack.append(self.rnn_builders[l].initial_state())

        if self.mc.add_nl_layer:
            self.MLPW_ex = self.MLPW.expr()
            self.MLPB_ex = self.MLPB_par.expr()

        self.adapterW_ex = self.adapterW.expr()
        self.adapterB_ex = self.adapterB.expr()

    def __call__(self, sequence):
        if not hasattr(self, 'rnn_stack'):
            self.logger.info("RNN initialized")
            self.declare_expr()

        rnn_ex = self.rnn_stack[0].add_input(sequence).output()
        if self.layers > 1:
            for l in range(1, self.layers):
                rnn_ex = rnn_ex + self.rnn_stack[l].add_input(rnn_ex).output()

        if self.mc.add_nl_layer:
            rnn_ex = (self.MLPW_ex * rnn_ex) + self.MLPB_ex
            rnn_ex = dy.tanh(rnn_ex)

        out_ex = (self.adapterW_ex * rnn_ex) + self.adapterB_ex
        return out_ex


class ESRNN(object):
    def __init__(self, logger=None, mc_yaml_dir=os.path.join(os.path.dirname(__file__), 'config_esrnn.yaml')):

        mc = ModelConfig(config_file=mc_yaml_dir, root_dir='./')
        self.mc = mc

        self.logger = logger if logger else logging.getLogger(__name__)

    def compute_levels_seasons(self, ts_object):
        return self.es.compute_levels_seasons(ts_object)

    def declare_expr(self, ts_object):
        self.es.declare_expr(ts_object)
        self.rnn.declare_expr()

    def predict_serie(self, ts_object):
        dy.renew_cg()  # new computation graph
        self.declare_expr(ts_object)
        levels_ex, seasonalities_ex, log_diff_of_levels = self.compute_levels_seasons(ts_object)

        # Loop not needed, i is the last train observation
        i = len(ts_object.y)
        input_start = i - self.mc.input_size
        input_end = i

        # Deseasonalization and normalization
        input_ex = ts_object.y[input_start:input_end]
        input_ex = dy_arrInput(input_ex)
        season_ex = seasonalities_ex[input_start:input_end]
        season_ex = dy.concatenate(season_ex)
        input_ex = dy.cdiv(input_ex, season_ex)
        input_ex = dy.cdiv(input_ex, levels_ex[i - 1])
        input_ex = dy.log(input_ex)

        # ERASED Concatenate categories
        # categories_ex = dy_arrInput(ts_object.categories_vect)
        # input_ex = dy.concatenate([input_ex, categories_ex])

        output_ex = self.rnn(input_ex)

        # Seasonalization and leveling
        season_start = i
        season_end = i + self.mc.output_size
        season_ex = seasonalities_ex[season_start:season_end]
        season_ex = dy.concatenate(season_ex)

        output_ex = dy.cmult(dy.exp(output_ex), season_ex) * levels_ex[i - 1]

        return output_ex

    def get_dir_name(self, root_dir=None):
        if not root_dir:
            assert self.mc.root_dir
            root_dir = self.mc.root_dir

        data_dir = self.mc.dataset_name
        model_parent_dir = os.path.join(root_dir, data_dir)
        model_path = ['num_series_{}'.format(self.mc.max_num_series),
                      'lr_{}'.format(self.mc.learning_rate),
                      str(self.mc.copy)]
        model_dir = os.path.join(model_parent_dir, '_'.join(model_path))
        return model_dir

    def save(self, model_dir=None, copy=None):
        if copy is not None:
            self.mc.copy = copy

        if not model_dir:
            assert self.mc.root_dir
            model_dir = self.get_dir_name()

        if not os.path.exists(model_dir):
            os.makedirs(model_dir)

        rnn_filepath = os.path.join(model_dir, "rnn.model")
        es_filepath = os.path.join(model_dir, "es.model")

        self.logger.info('Saving model to:\n {}'.format(model_dir) + '\n')
        self.rnn.pc.save(rnn_filepath)
        self.es.pc.save(es_filepath)

    def load(self, model_dir=None, copy=None):
        if copy is not None:
            self.mc.copy = copy

        if not model_dir:
            assert self.mc.root_dir
            model_dir = self.get_dir_name()

        rnn_filepath = os.path.join(model_dir, "rnn.model")
        es_filepath = os.path.join(model_dir, "es.model")
        path = Path(rnn_filepath)

        if path.is_file():
            self.logger.info('Loading model from:\n {}'.format(model_dir) + '\n')
            self.rnn.pc.populate(rnn_filepath)
            self.es.pc.populate(es_filepath)
        else:
            self.logger.error('Model path {} does not exist'.format(path))

    def pinball_loss(self, y, y_hat, tau=0.5):
        """Computes the pinball loss between y and y_hat.
        y: actual values
        y_hat: predicted values
        tau: a float between 0 and 1 the slope of the pinball loss. In the context
        of quantile regression, the value of alpha determine the conditional
        quantile level.
        return: pinball_loss
        """
        delta_y = y - y_hat
        pinball = dy.bmax(tau * delta_y, (tau - 1) * delta_y)
        pinball = dy.mean_elems(pinball)
        return pinball

    def level_variability_loss(self, y, level_variability_penalty):
        level_var_loss = []
        for i in range(1, len(y)):
            diff_ex = y[i] - y[i - 1]
            level_var_loss.append(diff_ex * diff_ex)
        level_var_loss_ex = dy.average(level_var_loss)
        level_var_loss_ex *= level_variability_penalty
        return level_var_loss_ex

    def train(self):
        self.logger.info(10 * '=' + ' Training esrnn ' + 10 * '=')

        # Trainers
        per_series_trainer = dy.AdamTrainer(self.es.pc, alpha=self.mc.learning_rate * self.mc.per_series_lr_multip,
                                            beta_1=0.9, beta_2=0.999, eps=self.mc.gradient_eps)
        per_series_trainer.set_clip_threshold(self.mc.gradient_clipping_threshold)

        trainer = dy.AdamTrainer(self.rnn.pc, alpha=self.mc.learning_rate,
                                 beta_1=0.9, beta_2=0.999, eps=self.mc.gradient_eps)
        trainer.set_clip_threshold(self.mc.gradient_clipping_threshold)

        # training code
        for epoch in range(self.mc.max_epochs):
            start = time.time()
            forecast_losses = []
            lev_variability_losses = []
            state_losses = []

            for ts_object in self.X_train_tsobject:
                dy.renew_cg()  # new computation graph

                self.declare_expr(ts_object)
                levels_ex, seasonalities_ex, log_diff_of_levels = self.compute_levels_seasons(ts_object)

                losses = []
                for i in range(self.mc.input_size - 1, len(ts_object.y) - self.mc.output_size):
                    input_start = i + 1 - self.mc.input_size
                    input_end = i + 1

                    # Deseasonalization and normalization
                    input_ex = ts_object.y[input_start:input_end]
                    input_ex = dy_arrInput(input_ex)
                    season_ex = seasonalities_ex[input_start:input_end]
                    season_ex = dy.concatenate(season_ex)
                    input_ex = dy.cdiv(input_ex, season_ex)
                    input_ex = dy.cdiv(input_ex, levels_ex[i])
                    input_ex = dy.noise(dy.log(input_ex), self.mc.noise_std)

                    # ERASED Concatenate categories
                    # categories_ex = dy_arrInput(ts_object.categories_vect)
                    # input_ex = dy.concatenate([input_ex, categories_ex])

                    output_ex = self.rnn(input_ex)

                    labels_start = i + 1
                    labels_end = i + 1 + self.mc.output_size

                    # Deseasonalization and normalization
                    labels_ex = ts_object.y[labels_start:labels_end]
                    labels_ex = dy_arrInput(labels_ex)
                    season_ex = seasonalities_ex[labels_start:labels_end]
                    season_ex = dy.concatenate(season_ex)
                    labels_ex = dy.cdiv(labels_ex, season_ex)
                    labels_ex = dy.cdiv(dy.log(labels_ex), levels_ex[i])

                    loss_ex = self.pinball_loss(labels_ex, output_ex)
                    losses.append(loss_ex)

                # Losses
                forecloss_ex = dy.average(losses)
                loss_ex = forecloss_ex
                forecast_losses.append(forecloss_ex.npvalue())

                if self.mc.level_variability_penalty > 0:
                    level_var_loss_ex = self.level_variability_loss(log_diff_of_levels,
                                                                    self.mc.level_variability_penalty)
                    loss_ex += level_var_loss_ex
                    lev_variability_losses.append(level_var_loss_ex.npvalue())

                loss_ex.backward()
                try:
                    trainer.update()
                    per_series_trainer.update()
                except:
                    self.logger.error('Fail updating trainer in series: {}'.format(self.unique_ids[j]))
                    self.logger.error(ts_object.y)
                    levels_np = [level.npvalue() for level in levels_ex]
                    self.logger.error('Levels: {}'.format(levels_np))
                    self.logger.error('Min level: {}'.format(min(levels_np)))

                    season_np = [season.npvalue() for season in season_ex]
                    self.logger.error('Seasons: {}'.format(season_np))
                    self.logger.error('Min season: {}'.format(min(season_np)))

                    self.logger.error('Level_var_loss: {}'.format(level_var_loss_ex.npvalue()))

                    self.logger.error('Seasonality parameter: {}'.format(self.es.seas_sms_ex.npvalue()))
                    self.logger.error('Level parameter: {}'.format(self.es.lev_sms_ex.npvalue()))

                    # self.es.pc.reset_gradient()
                    # self.rnn.pc.reset_gradient()

            self.logger.info("========= Epoch {} finished =========".format(epoch))
            self.logger.info("Training time: {}".format(time.time() - start))
            self.logger.info("Forecast loss: {}".format(np.mean(forecloss_ex.npvalue())))

        self.logger.info('Train finished')

    def get_trainable_ids(self, X):
        """
        ToDo: Completar
        """
        train_ids = X['unique_id'].unique()
        return train_ids

    def panel_to_tsobject(self):
        """
        Receives panel and creates ts_object list.
        Parameters:
            X: SORTED array-like or sparse matrix, shape (n_samples, n_features)
                Test or validation data for panel, with column 'unique_id', date stamp 'ds' and 'y'.
        Returns:
            tsobject_list : list of ts objects
        """

        # ts_object list panel
        tsobject_list = []
        for i, idx in enumerate(self.unique_ids):
            # Fast filter X and y by id.
            top_row = np.asscalar(self.X['unique_id'].searchsorted(idx, 'left'))
            bottom_row = np.asscalar(self.X['unique_id'].searchsorted(idx, 'right'))
            y = self.X[top_row:bottom_row]['y'].values
            ds = self.X[top_row:bottom_row]['ds'].values

            m4_object = M4TS(self.mc, y, ds, i)
            tsobject_list.append(m4_object)

        return tsobject_list

    def fit(self, X):
        # Get trainable ids
        trainable_ids = self.get_trainable_ids(X)
        X = X.loc[X['unique_id'].isin(trainable_ids), :]

        # Sort X by unique_id for faster loop
        X = X.sort_values(by=['unique_id', 'ds']).reset_index(drop=True)

        # List of uniques ids
        self.X = X
        self.unique_ids = X['unique_id'].unique()
        self.X_train_tsobject = self.panel_to_tsobject()

        # Initialize models
        self.mc.max_num_series = len(self.unique_ids)
        self.es = ES(self.mc)
        self.rnn = LSTM(self.mc)

        # Train model
        self.train()

    def predict(self):
        """
            Predictions for all stored time series
        Returns:
            Y_hat_panel : array-like (n_samples, 1).
                Predicted values for models in Family for ids in Panel.
            ds: Corresponding list of date stamps
            unique_id: Corresponding list of unique_id
        """
        # Predictions for panel.
        Y_hat_panel = pd.DataFrame(columns=["unique_id", "ds", "y_hat"])

        for i, idx in enumerate(self.unique_ids):
            # Corresponding train ts_object
            ts_object = self.X_train_tsobject[i]

            # Asserts
            assert ts_object.id == i

            # Declare y_hat_id placeholder
            Y_hat_id = pd.DataFrame(np.zeros(shape=(self.mc.output_size, 1)), columns=["y_hat"])
            # Prediction
            y_hat = self.predict_serie(ts_object).npvalue()
            Y_hat_id.iloc[:, 0] = y_hat

            # Serie prediction
            Y_hat_id["unique_id"] = idx
            ds = date_range = pd.date_range(start=ts_object.last_ds, periods=self.mc.output_size + 1,
                                            freq=self.mc.frequency)
            Y_hat_id["ds"] = ds[1:]
            Y_hat_panel = Y_hat_panel.append(Y_hat_id, sort=False).reset_index(drop=True)

        return Y_hat_panel