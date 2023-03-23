import os
import random
import numpy as np
import torch
import torch.utils.data
import glob
import soundfile as sf
import librosa
import torchaudio

class MelLoader(torch.utils.data.Dataset):
    def __init__(self, filelist, hps, return_name=False):
        self.return_name = return_name
        self.max_length = hps.max_length
        self.min_len = hps.min_length
        self.hop_length = hps.hop_length
        with open(filelist) as f:
            lines = f.readlines()
            self.npys = [line.rstrip().split('|')[0] for line in lines]
            self.lengths = [int(line.rstrip().split('|')[1]) for line in lines]
            self.lengths = [length if length <= hps.max_length else hps.max_length for length in self.lengths]
        random.seed(1234)
        random.shuffle(self.npys)
        print("Total number of npys: {}".format(len(self.npys)))

    def __getitem__(self, index):
        mel = np.load(self.npys[index])
        mel = torch.FloatTensor(mel)
        length = mel.size(1)
        if length >= self.max_length:
            max_mel_start = length - self.max_length
            mel_start = random.randint(0, max_mel_start)
            mel = mel[:, mel_start:mel_start + self.max_length]
        return mel, None

    def __len__(self):
        return len(self.npys)

class MelCollate():
    def __init__(self, hps, return_name=False):
        self.return_name = return_name
        self.hop_length = hps.hop_length
        self.max_len = hps.max_length

    def __call__(self, batch):
        _, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([x[0].size(1) for x in batch]),
            dim=0, descending=True)
        mel_lengths = torch.LongTensor(len(batch))
        mel_padded = torch.FloatTensor(len(batch), batch[0][0].size(0), self.max_len)
        mel_padded.zero_()
        for i in range(len(ids_sorted_decreasing)):
            row = batch[ids_sorted_decreasing[i]]
            mel = row[0]
            mel_padded[i, :, :mel.size(1)] = mel
            mel_lengths[i] = mel.size(1)
        return mel_padded, mel_lengths


class AudioMelMixedMaskRandomLoader(torch.utils.data.Dataset):
    def __init__(self, filelist, hps, return_name=False):
        self.return_name = return_name
        self.return_name = return_name
        self.max_length = hps.max_length
        self.min_len = hps.min_length
        self.hop_length = hps.hop_length
        self.mask_value = hps.mask_value

        with open(filelist) as f:
            lines = f.readlines()
            self.npzs = [line.rstrip().split('|')[0] for line in lines]
            self.lengths = [int(line.rstrip().split('|')[1]) for line in lines]
            self.lengths = [length if length <= hps.max_length else hps.max_length for length in self.lengths]
        random.seed(1234)
        random.shuffle(self.npzs)
        print("Total number of npzs: {}".format(len(self.npzs)))

    def __getitem__(self, index):
        x = np.load(self.npzs[index])
        audio = torch.FloatTensor(x['audio'].astype(np.float32)).unsqueeze(0)
        mel = torch.FloatTensor(x['mel'])

        length = mel.size(1)

        if length >= self.max_length:
            max_spec_start = length - self.max_length
            spec_start = random.randint(0, max_spec_start)
            mel = mel[:, spec_start:spec_start + self.max_length]
            audio = audio[:, spec_start*self.hop_length:(spec_start + self.max_length)*self.hop_length]

        masked_mel = mel.clone().detach()
        rannum = random.randint(32,128)
        masked_mel[rannum:, :] = self.mask_value


        if self.return_name:
            return mel, audio, os.path.basename(self.npzs[index]).replace('.npz','.wav')
        return masked_mel, audio, mel

    def __len__(self):
        return len(self.npzs)


class DistributedBucketSampler(torch.utils.data.distributed.DistributedSampler):
    """
    Maintain similar input lengths in a batch.
    Length groups are specified by boundaries.
    Ex) boundaries = [b1, b2, b3] -> any batch is included either {x | b1 < length(x) <=b2} or {x | b2 < length(x) <= b3}.

    It removes samples which are not included in the boundaries.
    Ex) boundaries = [b1, b2, b3] -> any x s.t. length(x) <= b1 or length(x) > b3 are discarded.
    """

    def __init__(self, dataset, batch_size, boundaries, num_replicas=None, rank=None, shuffle=True):
        super().__init__(dataset, num_replicas=num_replicas, rank=rank, shuffle=shuffle)
        self.lengths = dataset.lengths
        self.batch_size = batch_size
        self.boundaries = boundaries

        self.buckets, self.num_samples_per_bucket = self._create_buckets()
        self.total_size = sum(self.num_samples_per_bucket)
        self.num_samples = self.total_size // self.num_replicas

    def _create_buckets(self):
        buckets = [[] for _ in range(len(self.boundaries) - 1)]
        for i in range(len(self.lengths)):
            length = self.lengths[i]
            idx_bucket = self._bisect(length)
            if idx_bucket != -1:
                buckets[idx_bucket].append(i)

        for i in range(len(buckets) - 1, 0, -1):
            if len(buckets[i]) == 0:
                buckets.pop(i)
                self.boundaries.pop(i + 1)

        num_samples_per_bucket = []
        for i in range(len(buckets)):
            len_bucket = len(buckets[i])
            total_batch_size = self.num_replicas * self.batch_size
            rem = (total_batch_size - (len_bucket % total_batch_size)) % total_batch_size
            num_samples_per_bucket.append(len_bucket + rem)
        return buckets, num_samples_per_bucket

    def __iter__(self):
        # deterministically shuffle based on epoch
        g = torch.Generator()
        g.manual_seed(self.epoch)

        indices = []
        if self.shuffle:
            for bucket in self.buckets:
                indices.append(torch.randperm(len(bucket), generator=g).tolist())
        else:
            for bucket in self.buckets:
                indices.append(list(range(len(bucket))))

        batches = []
        for i in range(len(self.buckets)):
            bucket = self.buckets[i]
            len_bucket = len(bucket)
            ids_bucket = indices[i]
            num_samples_bucket = self.num_samples_per_bucket[i]

            # add extra samples to make it evenly divisible
            rem = num_samples_bucket - len_bucket
            ids_bucket = ids_bucket + ids_bucket * (rem // len_bucket) + ids_bucket[:(rem % len_bucket)]

            # subsample
            ids_bucket = ids_bucket[self.rank::self.num_replicas]

            # batching
            for j in range(len(ids_bucket) // self.batch_size):
                batch = [bucket[idx] for idx in ids_bucket[j * self.batch_size:(j + 1) * self.batch_size]]
                batches.append(batch)

        if self.shuffle:
            batch_ids = torch.randperm(len(batches), generator=g).tolist()
            batches = [batches[i] for i in batch_ids]
        self.batches = batches

        assert len(self.batches) * self.batch_size == self.num_samples
        return iter(self.batches)

    def _bisect(self, x, lo=0, hi=None):
        if hi is None:
            hi = len(self.boundaries) - 1

        if hi > lo:
            mid = (hi + lo) // 2
            if self.boundaries[mid] < x and x <= self.boundaries[mid + 1]:
                return mid
            elif x <= self.boundaries[mid]:
                return self._bisect(x, lo, mid)
            else:
                return self._bisect(x, mid + 1, hi)
        else:
            return -1

    def __len__(self):
        return self.num_samples // self.batch_size






class AudioMelMaskCollate():
    def __init__(self, hps, return_name=False):
        self.return_name = return_name
        self.hop_len = hps.hop_length
        self.max_len = hps.max_length

    def __call__(self, batch):
        _, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([x[0].size(1) for x in batch]),
            dim=0, descending=True)

        masked_mel_lengths = torch.LongTensor(len(batch))
        wav_lengths = torch.LongTensor(len(batch))

        masked_mel_padded = torch.FloatTensor(len(batch), batch[0][0].size(0), self.max_len)
        wav_padded = torch.FloatTensor(len(batch), 1, self.max_len * self.hop_len)
        raw_padded = torch.FloatTensor(len(batch), batch[0][0].size(0), self.max_len)

        masked_mel_padded.zero_()
        wav_padded.zero_()
        raw_padded.zero_()

        for i in range(len(ids_sorted_decreasing)):
            row = batch[ids_sorted_decreasing[i]]

            masked_mel = row[0]
            masked_mel_padded[i, :, :masked_mel.size(1)] = masked_mel
            masked_mel_lengths[i] = masked_mel.size(1)

            wav = row[1]
            wav_padded[i, :, :wav.size(1)] = wav
            wav_lengths[i] = wav.size(1)

            raw = row[2]
            raw_padded[i, :, :raw.size(1)] = raw

        if self.return_name:
            names = []
            for i in range(len(ids_sorted_decreasing)):
                row = batch[ids_sorted_decreasing[i]]
                names.append(row[-1])
            return masked_mel_padded, masked_mel_lengths, wav_padded, wav_lengths, names
        return masked_mel_padded, masked_mel_lengths, wav_padded, wav_lengths, raw_padded


class InpaintMelAudioLoader(torch.utils.data.Dataset):
    def __init__(self, npz_path, hparams, return_name=False):
        self.npzs = self.get_npz_path(npz_path)
        self.return_name = return_name
        self.max_length = hparams.max_length
        self.min_len = hparams.min_length
        self.hop_length = hparams.hop_length
        self.npzs.sort()
        print("Total number of npzs: {}".format(len(self.npzs)))

    def get_npz_path(self, npz_path):
        npzs = glob.glob(os.path.join(npz_path, '**/*.npz'), recursive=True)
        return npzs



    def __getitem__(self, index):
        x = np.load(self.npzs[index])
        mel = torch.FloatTensor(x['mel'])
        ssr = self.npzs[index].split('/')[-3]
        basename = os.path.basename(self.npzs[index]).replace('.npz', '.wav')
        name = os.path.join(ssr, basename)
        mel_orig = torch.FloatTensor(x['mel_orig'])
        gt = torch.FloatTensor(x['audio_orig'])

        gt_save = (gt * 32768.0).numpy().astype('int16')
        audio_save = (x['audio'] * 32768.0).astype('int16')

        gt_name = os.path.join('./logs/results/gt', basename)
        src_name = os.path.join('./logs/results/src', ssr, basename)

        os.makedirs(os.path.dirname(gt_name), exist_ok=True)
        os.makedirs(os.path.dirname(src_name), exist_ok=True)
        sf.write(gt_name, gt_save, samplerate=24000)
        sf.write(src_name, audio_save, samplerate=24000)
        return mel, mel_orig, gt, name
    def __len__(self):
        return len(self.npzs)

class InpaintMelAudioCollate():
    def __init__(self, hps, return_name=False, multi=False):
        self.return_name = return_name
        self.hop_len = hps.hop_length
        self.max_len = hps.max_length

    def __call__(self, batch):
        _, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([x[0].size(1) for x in batch]),
            dim=0, descending=True)

        mel_lengths = torch.LongTensor(len(batch))
        mel_padded = torch.FloatTensor(len(batch), batch[0][0].size(0), self.max_len)
        mel_padded.zero_()

        orig_lengths = torch.LongTensor(len(batch))
        orig_padded = torch.FloatTensor(len(batch), batch[0][0].size(0), self.max_len)
        orig_padded.zero_()

        gt_lengths = torch.LongTensor(len(batch))
        gt_padded = torch.FloatTensor(len(batch), self.max_len * self.hop_len)
        gt_padded.zero_()

        names = []
        for i in range(len(ids_sorted_decreasing)):
            row = batch[ids_sorted_decreasing[i]]

            mel = row[0]
            mel_padded[i, :, :mel.size(1)] = mel
            mel_lengths[i] = mel.size(1)

            names.append(row[-1])

            orig = row[1]
            orig_padded[i, :, :orig.size(1)] = orig
            orig_lengths[i] = orig.size(1)

            gt = row[2]
            gt_padded[i, :gt.size(0)] = gt
            gt_lengths[i] = gt.size(0)


        return mel_padded, mel_lengths, orig_padded, orig_lengths, gt_padded, gt_lengths, names




class InpaintAudioLoader(torch.utils.data.Dataset):
    def __init__(self, file_path, output_dir, hparams, return_name=False):
        if os.path.splitext(file_path)[1] != '':
            self.files = [file_path]
        else:
            self.files = glob.glob(os.path.join(file_path, '**/*'), recursive=True)
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.return_name = return_name
        self.max_length = hparams.max_length
        self.min_len = hparams.min_length
        self.hop_length = hparams.hop_length
        self.samplerate = hparams.sampling_rate
        self.files.sort()
        print("Total number of files: {}".format(len(self.files)))

    def __getitem__(self, index):
        audio, sr = torchaudio.load(self.files[index])
        audio = torchaudio.functional.resample(audio, sr, self.samplerate, resampling_method='kaiser_window').squeeze(0)
        audio = audio / torch.abs(audio).max() * 0.95
        basename = os.path.splitext(os.path.basename(self.files[index]))[0]
        name = os.path.join(self.output_dir, basename+'.wav')

        return torch.FloatTensor(audio), name
    def __len__(self):
        return len(self.files)

class InpaintAudioCollate():
    def __init__(self, hps, return_name=False, multi=False):
        self.return_name = return_name
        self.hop_len = hps.hop_length
        self.max_len = hps.max_length

    def __call__(self, batch):
        audio, names = batch[0]
        audio_segs = list(audio.split(self.hop_len * self.max_len))

        audio_padded = torch.FloatTensor(len(audio_segs), self.hop_len * self.max_len)
        audio_padded.zero_()
        audio_lengths = [seg.size(-1) for seg in audio_segs]
        audio_lengths = torch.LongTensor(audio_lengths)

        for i, seg in enumerate(audio_segs):
            audio_padded[i][:seg.size(-1)] = seg

        return audio_padded, audio_lengths, names