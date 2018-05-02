import csv
import math
import nltk
import numpy as np
import os
import pprint
import pickle
import string
import torch

from torch.utils.data import Dataset
from torch.utils.data.sampler import Sampler

if not os.path.exists(os.path.join(os.path.expanduser('~'), 'nltk_data')):
    nltk.download('punkt')


class NETSDataset(object):
    def __init__(self, _config, pretrained_dict):
        self.config = _config
        self.initial_settings()
        self.initialize_dictionary()

        # only for stats
        self.week_key_set = set()

        assert pretrained_dict is not None

        # word
        self.word2idx = pretrained_dict['word2idx']
        self.idx2word = pretrained_dict['idx2word']
        self.widx2vec = pretrained_dict['widx2vec']

        # char
        self.char2idx = pretrained_dict['char2idx']
        self.idx2char = pretrained_dict['idx2char']

        # duration
        self.dur2idx = pretrained_dict['dur2idx']
        self.idx2dur = pretrained_dict['idx2dur']

        # user
        self.user2idx = pretrained_dict['user2idx']
        self.idx2user = pretrained_dict['idx2user']

        # max len
        self.config.max_sentlen = pretrained_dict['config.max_sentlen']
        self.config.max_wordlen = pretrained_dict['config.max_wordlen']

        self.config.char_vocab_size = len(self.char2idx)
        self.config.word_vocab_size = len(self.word2idx)
        self.config.user_size = len(self.user2idx)
        self.config.dur_size = len(self.dur2idx)
        self.config.slot_size = self.slot_size
        self.config.class_div = self.class_div

        self.train_data = None
        self.valid_data = None
        self.test_data = self.process_data(
                self.config.test_path)

        self.train_ptr = 0
        self.valid_ptr = 0
        self.test_ptr = 0
    
    def initial_settings(self):
        # predefined settings
        self.UNK = 'UNK'
        self.PAD = 'PAD'
        self.feature_len = 12
        self.duration_unit = 30  # min
        self.max_rs_dist = 2  # reg-st week distance
        self.class_div = 2  # 168 output
        self.slot_size = 336
        self.max_snapshot = float("inf")  # 35
        self.min_word_cnt = 0
        self.max_title_len = 50
        self.max_word_len = 50
        self.max_event_cnt = 5000

    def initialize_dictionary(self):
        # dictionary specific settings
        self.char2idx = {}
        self.idx2char = {}
        self.word2idx = {}
        self.idx2word = {}
        self.widx2vec = []  # pretrained
        self.user2idx = {}
        self.idx2user = {}
        self.dur2idx = {}
        self.idx2dur = {}
        self.char2idx[self.PAD] = self.word2idx[self.PAD] = 0
        self.char2idx[self.UNK] = self.word2idx[self.UNK] = 1
        self.idx2char[0] = self.idx2word[0] = self.PAD
        self.idx2char[1] = self.idx2word[1] = self.UNK
        self.user2idx[self.UNK] = 0
        self.idx2user[0] = self.UNK
        self.dur2idx[self.UNK] = 0
        self.idx2dur[0] = self.UNK
        self.initial_word_dict = {}
        self.invalid_weeks = []
        self.user_event_cnt = {}
    
    def update_dictionary(self, key, mode=None):
        # update dictionary given a key
        if mode == 'c':
            if key not in self.char2idx:
                self.char2idx[key] = len(self.char2idx)
                self.idx2char[len(self.idx2char)] = key
        elif mode == 'w':
            if key not in self.word2idx:
                self.word2idx[key] = len(self.word2idx)
                self.idx2word[len(self.idx2word)] = key
        elif mode == 'u':
            if key not in self.user2idx:
                self.user2idx[key] = len(self.user2idx)
                self.idx2user[len(self.idx2user)] = key
        elif mode == 'd':
            if key not in self.dur2idx:
                self.dur2idx[key] = len(self.dur2idx)
                self.idx2dur[len(self.idx2dur)] = key
    
    def map_dictionary(self, key_list, dictionary, reverse=False):
        # mapping list of keys into dictionary 
        #   reverse=False : word2idx, char2idx
        #   reverse=True : idx2word, idx2char
        output = []
        for key in key_list:
            if key in dictionary:
                # skip PAD for reverse
                if reverse and key == self.word2idx[self.PAD]:
                    continue
                else:
                    output.append(dictionary[key])
            else:  # unknown key
                if not reverse:
                    output.append(dictionary[self.UNK])
                else:
                    output.append(dictionary[self.word2idx[self.UNK]])
        return output
    
    def build_word_dict(self, path, update=True):
        print('### build word dict %s' % path)
        with open(path, 'r', newline='', encoding='utf-8') as f:
            calendar_data = csv.reader(f, quotechar='"')
            prev_what_list = []
            prev_week_key = ''
            for k, features in enumerate(calendar_data):
                assert len(features) == self.feature_len 
                what = features[1]
                user_id = features[0]
                st_year = features[5]
                st_week = features[6]
                reg_seq = int(features[7])
                week_key = '_'.join([user_id, st_year, st_week])

                def check_printable(text, w_key):
                    for char in text:
                        if char not in string.printable:
                            if w_key not in self.invalid_weeks:
                                self.invalid_weeks.append(w_key)
                            return False
                    return True
                
                def check_maxlen(text, w_key):
                    _what_split = nltk.word_tokenize(text)
                    if len(_what_split) > self.max_title_len:
                        if w_key not in self.invalid_weeks:
                            self.invalid_weeks.append(w_key)
                            return False
                    for _word in _what_split:
                        if len(_word) > self.max_word_len:
                            if w_key not in self.invalid_weeks:
                                self.invalid_weeks.append(w_key)
                                return False
                    return True

                if reg_seq == 0:
                    assert prev_week_key != week_key
                    # process previous week's what list
                    if prev_week_key not in self.invalid_weeks and update:
                        for single_what in prev_what_list:
                            what_split = nltk.word_tokenize(single_what)
                            if self.config.word2vec_type == 6:
                                what_split = [w.lower() for w in what_split]
                            for word in what_split:
                                if word not in self.initial_word_dict:
                                    self.initial_word_dict[word] = (
                                            len(self.initial_word_dict), 1)
                                else:
                                    self.initial_word_dict[word] = (
                                            self.initial_word_dict[word][0],
                                            self.initial_word_dict[word][1] + 1)

                    # first event should be also printable
                    if check_printable(what, week_key) \
                            and check_maxlen(what, week_key):
                        prev_what_list = [what]
                    else:
                        prev_what_list = []
                    prev_week_key = week_key
                else:
                    assert prev_week_key == week_key
                    if prev_week_key in self.invalid_weeks:
                        continue
                    
                    # event title should be printable
                    if check_printable(what, prev_week_key) \
                            and check_maxlen(what, prev_week_key):
                        prev_what_list.append(what)

        print('initial dict size', len(self.initial_word_dict))

    def get_pretrained_word(self, path):
        print('\n### load pretrained %s' % path)
        word2vec = dict()
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                cols = line.split(' ')
                if cols[0] in self.initial_word_dict:
                    word2vec[cols[0]] = [float(l) for l in cols[1:]]
        
        widx2vec = []
        unk_cnt = 0
        widx2vec.append([0.0] * self.config.word_embed_dim)  # UNK
        widx2vec.append([0.0] * self.config.word_embed_dim)  # PAD

        for word, (word_idx, word_cnt) in self.initial_word_dict.items():
            if word != 'UNK' and word != 'PAD':
                assert word_cnt > 0
                if word in word2vec and word_cnt > self.min_word_cnt:
                    self.update_dictionary(word, 'w')
                    widx2vec.append(word2vec[word])
                else:
                    unk_cnt += 1

        self.widx2vec = widx2vec

        print('pretrained vectors', np.asarray(widx2vec).shape, 'unk', unk_cnt)
        print('dictionary change', len(self.initial_word_dict),
              'to', len(self.word2idx), len(self.idx2word), end='\n\n')

    def process_data(self, path, update_dict=False):
        print('### processing %s' % path)
        total_data = []
        max_wordlen = max_sentlen = max_dur = max_snapshot = 0
        min_dur = float("inf")
        max_slot_idx = (self.slot_size // self.class_div) - 1

        with open(path, 'r', newline='', encoding='utf-8') as f:
            """
            Each line consists of features below:
                0: user id
                1: what
                2: duration (minute)
                3: register time
                4: start time
                5: start year
                6: start week
                7: register sequence in the week
                8: register start week distance
                9: register start day distance
                10: is recurrent?
                11: start time slot (y)
            """
            prev_user = ''
            prev_st_yw = ('', '')
            saved_snapshot = []
            calendar_data = csv.reader(f, quotechar='"')

            for k, features in enumerate(calendar_data):
                assert len(features) == self.feature_len
                user_id = features[0]
                what = features[1]
                duration = int(features[2])
                # reg_time = features[3]
                # st_time = features[4]
                st_year = features[5]
                st_week = features[6]
                reg_seq = int(features[7])
                reg_st_week_dist = int(features[8])
                # reg_st_day_dist = int(features[9])
                is_recurrent = features[10]
                st_slot = int(features[11])

                # remove unprintable weeks
                week_key = '_'.join([user_id, st_year, st_week])
                if week_key in self.invalid_weeks:
                    continue

                # ready for one week data
                curr_user = user_id
                curr_st_yw = (st_year, st_week)

                # filter user by event count
                if user_id in self.user_event_cnt:
                    if self.user_event_cnt[user_id] > self.max_event_cnt:
                        prev_user = curr_user
                        prev_st_yw = curr_st_yw
                        continue

                # ignore data that was written in future
                if reg_st_week_dist < 0:
                    prev_user = curr_user
                    prev_st_yw = curr_st_yw
                    continue

                input_user = self.user2idx[self.UNK]

                # process title feature
                what_split = nltk.word_tokenize(what)
                if self.config.word2vec_type == 6:
                    what_split = [w.lower() for w in what_split]
                for word in what_split:
                    max_wordlen = \
                        len(word) if len(word) > max_wordlen else max_wordlen
                max_sentlen = \
                    len(what_split) if len(what_split) > max_sentlen \
                    else max_sentlen

                if update_dict:
                    for char in what:
                        self.update_dictionary(char, 'c')
                if max_wordlen > self.config.max_wordlen:
                    self.config.max_wordlen = max_wordlen
                if max_sentlen > self.config.max_sentlen:
                    self.config.max_sentlen = max_sentlen
                
                sentchar = []
                for word in what_split:
                    sentchar.append(self.map_dictionary(word, self.char2idx))
                sentword = self.map_dictionary(what_split, self.word2idx)
                length = len(sentword)
                assert len(sentword) == len(sentchar)
                input_title = [sentchar, sentword, length]

                # process duration feature
                max_dur = max_dur if max_dur > duration else duration
                min_dur = min_dur if min_dur < duration else duration
                fine_duration = \
                    (duration//self.duration_unit) * self.duration_unit
                fine_duration += (int(duration % self.duration_unit > 0) *
                                  self.duration_unit)
                if duration % self.duration_unit == 0:
                    assert duration == fine_duration
                else:
                    assert fine_duration - duration < self.duration_unit

                if update_dict:
                    self.update_dictionary(fine_duration, 'd')
                input_duration = self.dur2idx[fine_duration]

                # TODO: process reg_time feature

                # process st_slot feature
                assert st_slot < self.slot_size
                input_slot = st_slot // self.class_div
                target_slot = st_slot // self.class_div
                
                # process snapshot
                if reg_seq == 0:  # start of a new week
                    assert curr_user != prev_user or curr_st_yw != prev_st_yw
                    prev_user = curr_user
                    prev_st_yw = curr_st_yw
                    # prev_grid = []
                    input_snapshot = []
                    saved_snapshot = [[input_title, fine_duration, input_slot]]
                else:  # same as the prev week
                    assert curr_user == prev_user and curr_st_yw == prev_st_yw
                    # input_snapshot = copy.deepcopy(saved_snapshot)
                    prev_grid = [svs[2] for svs in saved_snapshot] 
                    if input_slot in prev_grid:
                        continue
                    input_snapshot = saved_snapshot[:]
                    saved_snapshot.append(
                        [input_title, fine_duration, input_slot])

                # transform snapshot features into slot grid
                # snapshot slots w/ durations

                target_n_slot = \
                    int(math.ceil(input_duration / (30 * self.class_div)))
                targets_w_duration = list()
                for shift in range(target_n_slot):
                    if target_slot + shift >= max_slot_idx:
                        break
                    targets_w_duration.append(target_slot + shift)

                input_grid = list()
                for ips in input_snapshot:
                    n_slots = int(math.ceil(ips[1] / (30 * self.class_div)))
                    for slot_idx in range(n_slots):
                        slot = ips[2] + slot_idx
                        if slot >= max_slot_idx:
                            break

                        if slot in targets_w_duration \
                                or slot in input_grid:
                            continue

                        input_grid.append(slot)

                assert target_slot not in input_grid

                # filter by register distance & max_snapshot & recurrent
                if (reg_st_week_dist <= self.max_rs_dist
                        and len(input_snapshot) <= self.max_snapshot
                        and 'False' == is_recurrent):
                    max_snapshot = max_snapshot \
                        if max_snapshot > len(input_snapshot) \
                        else len(input_snapshot)
                    total_data.append(
                        [input_user, input_title, input_duration,
                         input_snapshot, input_grid, target_slot])

                    if user_id not in self.user_event_cnt:
                        self.user_event_cnt[user_id] = 1
                    else:
                        self.user_event_cnt[user_id] += 1

                    self.week_key_set.add(week_key)

        if update_dict:
            self.config.char_vocab_size = len(self.char2idx)
            self.config.word_vocab_size = len(self.word2idx)
            self.config.user_size = len(self.user2idx)
            self.config.dur_size = len(self.dur2idx)
            self.config.slot_size = self.slot_size
            self.config.class_div = self.class_div

        print('data size', len(total_data))
        print('max duration', max_dur)
        print('min duration', min_dur)
        print('max snapshot', max_snapshot)
        print('max wordlen', max_wordlen)
        print('max sentlen', max_sentlen, end='\n\n')

        return total_data

    def get_dataloader(self, batch_size=None, shuffle=True):
        if batch_size is None:
            batch_size = self.config.batch_size

        if self.train_data:
            train_dataset = Vectorize(self.train_data, self.config)
            train_sampler = SortedBatchSampler(train_dataset.lengths(),
                                               batch_size,
                                               shuffle=shuffle)
            train_loader = torch.utils.data.DataLoader(
                train_dataset,
                batch_size=batch_size,
                sampler=train_sampler,
                num_workers=self.config.data_workers,
                collate_fn=self.batchify,
                pin_memory=True,
            )
        else:
            train_loader = None

        if self.valid_data:
            valid_dataset = Vectorize(self.valid_data, self.config)
            valid_sampler = SortedBatchSampler(valid_dataset.lengths(),
                                               batch_size,
                                               shuffle=False)
            valid_loader = torch.utils.data.DataLoader(
                valid_dataset,
                batch_size=batch_size,
                sampler=valid_sampler,
                num_workers=self.config.data_workers,
                collate_fn=self.batchify,
                pin_memory=True,
            )
        else:
            valid_loader = None

        test_dataset = Vectorize(self.test_data, self.config)
        test_sampler = SortedBatchSampler(test_dataset.lengths(),
                                          batch_size,
                                          shuffle=False)
        test_loader = torch.utils.data.DataLoader(
            test_dataset,
            batch_size=batch_size,
            sampler=test_sampler,
            num_workers=self.config.data_workers,
            collate_fn=self.batchify,
            pin_memory=True,
        )

        return train_loader, valid_loader, test_loader

    def batchify(self, batch):
        users = torch.cat([example[0] for example in batch])
        durs = torch.cat([example[1] for example in batch])
        tcs = [example[2] for example in batch]
        tws = [example[3] for example in batch]
        tls = [example[4] for example in batch]
        stcs = [example[5] for example in batch]
        stws = [example[6] for example in batch]
        stls = [example[7] for example in batch]
        sdurs = [example[8] for example in batch]
        sslots = [example[9] for example in batch]
        grids = torch.cat([example[10].unsqueeze(0) for example in batch])
        targets = torch.cat([example[11] for example in batch])

        return (users, durs, tcs, tws, tls,
                stcs, stws, stls, sdurs, sslots, grids, targets)

    def get_train_class_counts(self):
        cnt_list = [0] * (self.slot_size // self.class_div)
        for td in self.train_data:
            cnt_list[td[5]] += 1

        assert len(self.train_data) == sum(cnt_list)

        return cnt_list

    def get_class_weights(self):
        cnt_list = self.get_train_class_counts()

        # http://scikit-learn.org/stable/modules/generated/sklearn.utils.class_weight.compute_class_weight.html
        n_classes = self.slot_size // self.class_div
        n_samples = sum(cnt_list)

        assert len(cnt_list) == n_classes

        return [n_samples / (n_classes * cnt) for cnt in cnt_list]


class Vectorize(Dataset):

    def __init__(self, examples, cfg):
        self.examples = examples
        self.config = cfg

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, index):
        example = self.examples[index]

        # user and duration
        user = torch.LongTensor([example[0]])
        dur = torch.LongTensor([example[2]])

        # Title (char, word, length) => will be converted to
        # tensors in the model (due to paddings)
        title = example[1]
        tc = title[0]
        tw = title[1]
        tl = title[2]

        # Snapshot (title, duration, slot)
        snapshot = example[3]
        stc = []
        stw = []
        stl = []
        sdur = []
        sslot = []
        for _, event in enumerate(snapshot):
            stc.append(event[0][0])
            stw.append(event[0][1])
            stl.append(event[0][2])
            sdur.append(event[1])
            sslot.append(event[2])

        # Grid
        grid = torch.zeros(
            self.config.sm_day_num * self.config.sm_slot_num)
        if len(example[4]) > 0:
            grid[example[4]] = 1

        # Target
        target = torch.LongTensor([example[5]])
        assert example[5] not in example[4], (example[4], example[5])

        return user, dur, tc, tw, tl, stc, stw, stl, sdur, sslot, grid, target

    def lengths(self):
        def maxlen_from_snapshot(snapshots):
            if len(snapshots) > 0:
                return max([s[0][2] for s in snapshots])
            else:
                return 0
        return [(example[1][2], maxlen_from_snapshot(example[3]))
                for example in self.examples]


class SortedBatchSampler(Sampler):

    def __init__(self, lengths, batch_size, shuffle=True):
        super(SortedBatchSampler, self).__init__(None)
        self.lengths = lengths
        self.batch_size = batch_size
        self.shuffle = shuffle

    def __iter__(self):
        lengths = np.array(
            [(l[0], l[1], np.random.random()) for l in self.lengths],
            dtype=[('l1', np.int_), ('l2', np.int_), ('rand', np.float_)]
        )
        indices = np.argsort(lengths, order=('l2', 'l1', 'rand'))
        batches = [indices[i:i + self.batch_size]
                   for i in range(0, len(indices), self.batch_size)]
        if self.shuffle:
            np.random.shuffle(batches)
        return iter([i for batch in batches for i in batch])

    def __len__(self):
        return len(self.lengths)


class Config(object):
    def __init__(self):
        path_base = './data'

        self.train_path = os.path.join(path_base, 'train.csv')
        self.valid_path = os.path.join(path_base, 'valid.csv')
        self.test_path = os.path.join(path_base, 'test.csv')
        # http://nlp.stanford.edu/data/glove.840B.300d.zip
        self.word2vec_path = \
            os.path.expanduser('~') + '/common/glove/glove.840B.300d.txt'
        self.word2vec_type = 840  # 6 or 840 (B)
        self.word_embed_dim = 300
        self.batch_size = 16
        self.max_wordlen = 0
        self.max_sentlen = 0
        self.char_vocab_size = 0
        self.word_vocab_size = 0
        self.user_size = 0
        self.dur_size = 0
        self.class_div = 0
        self.slot_size = 0
        self.data_workers = 5
        self.save_preprocess = False
        self.sm_day_num = 7
        self.sm_slot_num = 24
        self.preprocess_save_path = './data/preprocess_tmp.pkl'
        self.preprocess_load_path = './data/preprocess_20180429.pkl'


if __name__ == '__main__':
    config = Config()
    if config.save_preprocess:
        dataset = NETSDataset(config)
        pickle.dump(dataset, open(config.preprocess_save_path, 'wb'))
    else:
        print('## load preprocess %s' % config.preprocess_load_path)
        dataset = pickle.load(open(config.preprocess_load_path, 'rb'))
   
    # dataset config must be valid
    pprint.PrettyPrinter().pprint(
        ([(k, v) for k, v in vars(dataset.config).items() if '__' not in k]))
    print()

    class_counts = dataset.get_train_class_counts()
    print('class_counts', 'min', min(class_counts), 'max', max(class_counts))
    w = dataset.get_class_weights()
    print('class_weights', 'min', min(w), 'max', max(w))

    for d_idx, ex in enumerate(dataset.get_dataloader(batch_size=16)[0]):
        if d_idx % 100 == 0:
            print(d_idx)
    print('\niteration test pass!')
