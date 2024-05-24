# -*- coding: UTF-8 -*-
"""
@project:GDCN
"""
import math
import shutil
import struct
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import lmdb
import numpy as np
import torch.utils.data
from tqdm import tqdm


class CriteoDataset(torch.utils.data.Dataset):
    """
    Criteo Display Advertising Challenge Dataset

    Data prepration:
        * Remove the infrequent features (appearing in less than threshold instances) and treat them as a single feature
        * Discretize numerical values by log2 transformation which is proposed by the winner of Criteo Competition

    :param dataset_path: criteo train.txt path.
    :param cache_path: lmdb cache path.
    :param rebuild_cache: If True, lmdb cache is refreshed.
    :param min_threshold: infrequent feature threshold.

    Reference:
        https://labs.criteo.com/2014/02/kaggle-display-advertising-challenge-dataset
        https://www.csie.ntu.edu.tw/~r01922136/kaggle-2014-criteo.pdf
    """

    def __init__(self, dataset_path=None, cache_path='.criteo', rebuild_cache=False, min_threshold=10, is_test=False, feat_mapper=None, defaults=None):
        self.NUM_FEATS = 39
        self.NUM_INT_FEATS = 13
        self.min_threshold = min_threshold

        if is_test:
            self.prefix = "criteoTest/"
            if rebuild_cache or not Path(cache_path).exists():
                shutil.rmtree(cache_path, ignore_errors=True)
                if dataset_path is None:
                    raise ValueError('create cache: failed: dataset_path is None')
                self.__build_cache(dataset_path, cache_path, feat_mapper, defaults)
            self.env = lmdb.open(cache_path, create=False, lock=False, readonly=True)
            with self.env.begin(write=False) as txn:
                self.length = txn.stat()['entries'] - 1
                self.field_dims = np.frombuffer(txn.get(b'field_dims'), dtype=np.uint32)
        else:
            # The path of Criteo dataset
            # TODO
            self.prefix = "criteo/"
            #dataset_path = 'data/criteo/train_df_sampled_GDCN.csv'
            dataset_path = 'data/criteo/train_df_GDCN.csv'
            if rebuild_cache or not Path(cache_path).exists():
                shutil.rmtree(cache_path, ignore_errors=True)
                if dataset_path is None:
                    raise ValueError('create cache: failed: dataset_path is None')
                
                self.feat_mapper, self.defaults = self.__get_feat_mapper(dataset_path)
                self.__save_feat_mapper(self.feat_mapper, self.defaults)
                
                self.__build_cache(dataset_path, cache_path)
            else:
                self.feat_mapper, self.defaults = self.__load_feat_mapper()
            self.env = lmdb.open(cache_path, create=False, lock=False, readonly=True)
            with self.env.begin(write=False) as txn:
                self.length = txn.stat()['entries'] - 1
                self.field_dims = np.frombuffer(txn.get(b'field_dims'), dtype=np.uint32)

    def __getitem__(self, index):
        # Must be implemented to read data from the cache data
        with self.env.begin(write=False) as txn:
            np_array = np.frombuffer(
                txn.get(struct.pack('>I', index)), dtype=np.uint32).astype(dtype=np.long)
        return np_array[1:], np_array[0]

    def __len__(self):
        return self.length

    def __build_cache(self, path, cache_path, feat_mapper=None, defaults=None):
        #temp_path = 'data/' + self.prefix + "train_df_sampled_GDCN.csv"
        #temp_path = 'data/' + self.prefix + "test_df_GDCN.csv"
        temp_path = 'data/' + self.prefix + "train_df_GDCN.csv"
        print("temp_path", temp_path)

        feat_mapper, defaults = self.__get_feat_mapper(temp_path) if feat_mapper is None else (feat_mapper, defaults)

        # feat_mapper, defaults = self.__get_feat_mapper(temp_path)

        with lmdb.open(cache_path, map_size=int(1e11)) as env:
            field_dims = np.zeros(self.NUM_FEATS, dtype=np.uint32)
            for i, fm in feat_mapper.items():
                field_dims[i - 1] = len(fm) + 1

            with env.begin(write=True) as txn:
                txn.put(b'field_dims', field_dims.tobytes())

            for buffer in self.__yield_buffer(path, feat_mapper, defaults):
                with env.begin(write=True) as txn:
                    for key, value in buffer:
                        txn.put(key, value)

    def __get_feat_mapper(self, path):
        feat_cnts = defaultdict(lambda: defaultdict(int))
        print(path)
        with open(path) as f:
            f.readline()
            pbar = tqdm(f, mininterval=1, smoothing=0.1)
            pbar.set_description('Create criteo dataset cache: counting features')
            for line in pbar:
                values = line.rstrip('\n').split(',')

                if len(values) != self.NUM_FEATS + 1:
                    continue

                for i in range(1, self.NUM_INT_FEATS + 1):
                    feat_cnts[i][convert_numeric_feature(values[i])] += 1

                for i in range(self.NUM_INT_FEATS + 1, self.NUM_FEATS + 1):
                    feat_cnts[i][values[i]] += 1

        feat_mapper = {i: {feat for feat, c in cnt.items() if c >= self.min_threshold} for i, cnt in feat_cnts.items()}
        feat_mapper = {i: {feat: idx for idx, feat in enumerate(cnt)} for i, cnt in feat_mapper.items()}
        defaults = {i: len(cnt) for i, cnt in feat_mapper.items()}

        return feat_mapper, defaults

    def __yield_buffer(self, path, feat_mapper, defaults, buffer_size=int(1e5)):
        item_idx = 0
        buffer = list()
        #path = 'data/criteoTrainSampled/train_df_sampled_GDCN.csv'
        #path = 'data/criteoTest/test_df_GDCN.csv'
        path = 'data/criteo/train_df_GDCN.csv'
        with open(path) as f:
            f.readline()
            pbar = tqdm(f, mininterval=1, smoothing=0.1)
            pbar.set_description('Create criteo dataset cache: setup lmdb')
            for line in pbar:
                values = line.rstrip('\n').split(',')
                if len(values) != self.NUM_FEATS + 1:
                    continue
                np_array = np.zeros(self.NUM_FEATS + 1, dtype=np.uint32)
                np_array[0] = int(values[0])
                for i in range(1, self.NUM_INT_FEATS + 1):
                    np_array[i] = feat_mapper[i].get(convert_numeric_feature(values[i]), defaults[i])

                for i in range(self.NUM_INT_FEATS + 1, self.NUM_FEATS + 1):
                    np_array[i] = feat_mapper[i].get(values[i], defaults[i])
                buffer.append((struct.pack('>I', item_idx), np_array.tobytes()))
                item_idx += 1
                if item_idx % buffer_size == 0:
                    yield buffer
                    buffer.clear()
            yield buffer

    def __save_feat_mapper(self, feat_mapper, defaults):
        with open('feature_mapping/feat_mapper.npy', 'wb') as f:
            np.save(f, feat_mapper)
        with open('feature_mapping/defaults.npy', 'wb') as f:
            np.save(f, defaults)

    def __load_feat_mapper(self):
        with open('feature_mapping/feat_mapper.npy', 'rb') as f:
            feat_mapper = np.load(f, allow_pickle=True).item()
        with open('feature_mapping/defaults.npy', 'rb') as f:
            defaults = np.load(f, allow_pickle=True).item()
        return feat_mapper, defaults


@lru_cache(maxsize=None)
def convert_numeric_feature(val: str):
    """
         https://www.csie.ntu.edu.tw/~r01922136/kaggle-2014-criteo.pdf
    """
    if val == '':
        return 'NULL'
    v = float(val)
    if v > 2:
        return str(int(math.log(v) ** 2))
    else:
        return str(v - 2)


def get_criteo_dataloader_test(test_path="test_df_GDCN.csv", batch_size=2048):
    print("Start loading criteo test data....")
    prefix = "criteoTest/"
    test_path = "test_df_GDCN.csv"
    test_path = prefix + test_path

    with open('feature_mapping/feat_mapper.npy', 'rb') as f:
        feat_mapper = np.load(f, allow_pickle=True).item()
    with open('feature_mapping/defaults.npy', 'rb') as f:
        defaults = np.load(f, allow_pickle=True).item()

    dataset = CriteoDataset(dataset_path=test_path, cache_path='data/' + prefix + ".criteoTest", is_test=True, feat_mapper=feat_mapper, defaults=defaults)

    test_loader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=False)
    field_dims = dataset.field_dims

    return field_dims, test_loader


def get_criteo_dataloader_811(train_path="train.txt", batch_size=2048):
    # the test_path maybe null, if it is, we need to split the train dataset
    print("Start loading criteo data....")
    prefix = "criteo/"
    train_path = prefix + train_path
    #dataset = CriteoDataset(dataset_path=train_path, cache_path='data/' + prefix + ".criteoTrainSampled")
    dataset = CriteoDataset(dataset_path=train_path, cache_path='data/' + prefix + ".criteoTrain")

    # Save feature mappings after generating from training data
    feat_mapper, defaults = dataset.feat_mapper, dataset.defaults
    # dataset.__save_feat_mapper(feat_mapper, defaults)

    # Split the training data to 8:1:1
    all_length = len(dataset)
    test_size = int(0.1 * all_length)  # Equal to validation size
    train_size = all_length - test_size * 2

    train_dataset, test_dataset = torch.utils.data.random_split(dataset, [train_size, test_size * 2])
    test_dataset, valid_dataset = torch.utils.data.random_split(test_dataset, [test_size, test_size])
    # print("train_dataset length:", len(train_dataset))
    # print("valid_dataset length:", len(valid_dataset))
    # print("test_dataset length:", len(test_dataset))
    train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    valid_Loader = torch.utils.data.DataLoader(valid_dataset, batch_size=batch_size, shuffle=True)
    test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=batch_size, shuffle=False)
    field_dims = dataset.field_dims
    # print(field_dims)
    # print(sum(field_dims))

    return field_dims, train_loader, valid_Loader, test_loader
