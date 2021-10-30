import os
from typing import Callable, List, Optional, Tuple, Union

import pandas as pd
import numpy as np

# uncomment to turn off gpu (see https://stackoverflow.com/a/45773574)
# os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
import tensorflow as tf

from preprocessing import prepare_data_1_min, prepare_data_hourly, DataGen


def train_nn_models(
    solar: pd.DataFrame,
    sunspots: pd.DataFrame,
    dst: pd.DataFrame,
    model_definer: Callable[[], Tuple[tf.keras.Model, np.ndarray, int, float, int]],
    num_models: int = 1,
    output_folder: str = "trained_models",
    data_frequency: str = "minute",
    early_stopping: bool = False,
) -> Optional[List[float]]:
    """Train and save ensemble of models, each trained on a different subset of data.

    Args:
        solar: DataFrame containing solar wind data
        sunspots: DataFrame containing sunspots data
        dst: DataFrame containing the disturbance storm time (DST) data, i.e. the labels
            for training
        model_definer: Function returning keras model and initial weights
        num_models: Number of models to train.
            Training several models on different subsets of the data and averaging
            results improves model accuracy. If ``num_models = 1``, use all data for
            training. If ``num_models > 1``, each model will be trained on
            ``int((num_models - 1) / num_models) * num_months`` randomly-selected months
            of data, where ``num_months`` is the total number of months in the training
            data. Because the data contains long-term trends lasting days or weeks,
            splitting the data by month ensures that different models have sufficiently
            different data sets to obtain the benefits of model diversity. Separate
            models are trained for the current and next hour, so the number of models
            output will be ``num_models * 2``.
        output_folder: Path to the directory where models will be saved
        data_frequency: frequency of the training data: "minute" or "hour"
        early_stopping: If ``True``, stop model training when validation loss stops
            decreasing. If ``num_models > 1``, use out-of-fold data as the validation
            set; otherwise randomly select 20% of months. Early stopping is used
            only on the time ``t`` model, and the resulting optimal number of epochs is
            used to train the ``t + 1`` model.

    Returns:
        out-of-sample accuracy: If ``num_models > 1``, returns list of length
            ``num_models`` containing RMSE values for out-of-sample predictions for each
            model. These provide an indication of model accuracy, but in general will
            underestimate the accuracy of the full ensemble of models. If
            ``num_models = 1``, returns ``None``.
    """

    # prepare data
    if data_frequency == "minute":
        solar, train_cols = prepare_data_1_min(
            solar, sunspots, dst, output_folder=output_folder, norm_df=None
        )
    else:
        solar, train_cols = prepare_data_hourly(
            solar, sunspots, dst, output_folder=output_folder, norm_df=None
        )

    # define model and training parameters
    model, initial_weights, epochs, lr, bs = model_definer()
    if data_frequency == "minute":
        sequence_length = 6 * 24 * 7
    else:
        sequence_length = 24 * 7

    oos_accuracy = []
    # train on sequences ending at the start of an hour
    valid_bool = solar["timedelta"].dt.seconds % 3600 == 0
    # exclude periods where data contains nans
    nans_in_train = (
        solar[train_cols]
        .isnull()
        .any(axis=1)
        .rolling(window=sequence_length + 1, center=False)
        .max()
    )
    valid_bool = valid_bool & (nans_in_train == 0)
    np.random.seed(0)
    solar["month"] = solar["month"].astype(int)
    months = np.sort(solar["month"].unique())
    # remove the first week from each period, because not enough data for prediction
    valid_ind_arr = []
    for p in solar["period"].unique():
        all_p = solar.loc[(solar["period"] == p) & valid_bool].index.values[24 * 7 :]
        valid_ind_arr.append(all_p)
    valid_ind = np.concatenate(valid_ind_arr)
    non_exclude_ind = solar.loc[~solar["train_exclude"].astype(bool)].index.values
    np.random.shuffle(months)
    es_callback = tf.keras.callbacks.EarlyStopping(monitor="val_loss", patience=2,
                                                   restore_best_weights=True)
    for model_ind in range(num_models):
        # t model
        tf.keras.backend.clear_session()
        model.compile(
            loss=tf.keras.losses.MeanSquaredError(),
            optimizer=tf.keras.optimizers.Adam(learning_rate=lr),
            metrics=[tf.keras.metrics.RootMeanSquaredError()],
        )
        model.set_weights(initial_weights)
        if num_models > 1:
            # define train and test sets
            leave_out_months = months[
                model_ind
                * (len(months) // num_models) : (model_ind + 1)
                * (len(months) // num_models)
            ]
            leave_out_months_ind = solar.loc[
                valid_bool & solar["month"].isin(leave_out_months)
            ].index.values
            curr_months_ind = solar.loc[
                valid_bool & (~solar["month"].isin(leave_out_months))
            ].index.values
            train_ind = np.intersect1d(
                np.intersect1d(valid_ind, curr_months_ind), non_exclude_ind
            )
            test_ind = np.intersect1d(valid_ind, leave_out_months_ind)
            train_gen = DataGen(
                solar[train_cols].values,
                train_ind,
                solar["target"].values.flatten(),
                bs,
                sequence_length,
            )
            test_gen = DataGen(
                solar[train_cols].values,
                test_ind,
                solar["target"].values.flatten(),
                bs,
                sequence_length,
            )
            if early_stopping:
                model.fit(
                    train_gen,
                    validation_data=test_gen,
                    epochs=100,
                    verbose=1,
                    callbacks=[es_callback],
                )
            else:
                model.fit(train_gen, validation_data=test_gen, epochs=epochs, verbose=1)
            oos_accuracy.append(model.evaluate(test_gen, verbose=2)[1])
            print("Out of sample accuracy: ", oos_accuracy)
            print("Out of sample accuracy mean: {}".format(np.mean(oos_accuracy)))
        else:
            if early_stopping:
                leave_out_months = months[
                    model_ind
                    * (len(months) // 5) : (model_ind + 1)
                    * (len(months) // 5)
                ]
                leave_out_months_ind = solar.loc[
                    valid_bool & solar["month"].isin(leave_out_months)
                ].index.values
                curr_months_ind = solar.loc[
                    valid_bool & (~solar["month"].isin(leave_out_months))
                ].index.values
                train_ind = np.intersect1d(
                    np.intersect1d(valid_ind, curr_months_ind), non_exclude_ind
                )
                test_ind = np.intersect1d(valid_ind, leave_out_months_ind)
                train_gen = DataGen(
                    solar[train_cols].values,
                    train_ind,
                    solar["target"].values.flatten(),
                    bs,
                    sequence_length,
                )
                test_gen = DataGen(
                    solar[train_cols].values,
                    test_ind,
                    solar["target"].values.flatten(),
                    bs,
                    sequence_length,
                )
                model.fit(
                    train_gen,
                    validation_data=test_gen,
                    epochs=100,
                    verbose=1,
                    callbacks=[es_callback],
                )
            else:
                # fit on all data
                train_ind = valid_ind
                train_gen = DataGen(
                    solar[train_cols].values,
                    train_ind,
                    solar["target"].values.flatten(),
                    bs,
                    sequence_length,
                )
                model.fit(train_gen, epochs=epochs, verbose=1)
        model.save(os.path.join(output_folder, "model_t_{}.h5".format(model_ind)))
        # t + 1 model
        tf.keras.backend.clear_session()
        model.compile(
            loss=tf.keras.losses.MeanSquaredError(),
            optimizer=tf.keras.optimizers.Adam(learning_rate=lr),
            metrics=[tf.keras.metrics.RootMeanSquaredError()],
        )
        model.set_weights(initial_weights)
        data_gen = DataGen(
            solar[train_cols].values,
            train_ind,
            solar["target_shift"].values.flatten(),
            bs,
            sequence_length,
        )
        if early_stopping and (es_callback.stopped_epoch > 0):
            model.fit(data_gen, epochs=es_callback.stopped_epoch - es_callback.patience + 1,
                      verbose=1)
        else:
            model.fit(data_gen, epochs=epochs, verbose=1)
        model.save(
            os.path.join(output_folder, "model_t_plus_one_{}.h5".format(model_ind))
        )
        if num_models > 1:
            return oos_accuracy
