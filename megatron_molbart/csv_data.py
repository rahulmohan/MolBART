import torch
from torch.utils.data import Dataset
from pysmilesutils.augment import MolRandomizer, SMILESRandomizer
from pysmilesutils.datautils import BucketBatchSampler
from molbart.tokeniser import MolEncTokeniser
from molbart.util import DEFAULT_CHEM_TOKEN_START
from molbart.util import DEFAULT_VOCAB_PATH
from molbart.util import DEFAULT_MAX_SEQ_LEN
from molbart.util import REGEX
from molbart.util import load_tokeniser
from rdkit import Chem
import numpy as np
import pandas
from molbart.data.util import TokenSampler
from megatron.data.samplers import DistributedBatchSampler
from megatron import mpu
import torch
from pathlib import Path
import pandas as pd

tokenizer = MolEncTokeniser.from_vocab_file(DEFAULT_VOCAB_PATH, REGEX,
        DEFAULT_CHEM_TOKEN_START)
max_seq_len = 512


def check_seq_len(tokens, mask):
    """ Warn user and shorten sequence if the tokens are too long, otherwise return original

    Args:
        tokens (List[List[str]]): List of token sequences
        mask (List[List[int]]): List of mask sequences

    Returns:
        tokens (List[List[str]]): List of token sequences (shortened, if necessary)
        mask (List[List[int]]): List of mask sequences (shortened, if necessary)
    """

    seq_len = max([len(ts) for ts in tokens])
    if seq_len > max_seq_len:
        tokens_short = [ts[:max_seq_len] for ts in tokens]
        mask_short = [ms[:max_seq_len] for ms in mask]
        return (tokens_short, mask_short)
    return (tokens, mask)


def collate_fn(batch):
    """ Used by DataLoader to concatenate/collate inputs."""

    encoder_smiles = [x['encoder_smiles'][0] for x in batch]
    decoder_smiles = [x['decoder_smiles'][0] for x in batch]
    enc_token_output = tokenizer.tokenise(encoder_smiles, mask=True,
            pad=True)
    dec_token_output = tokenizer.tokenise(decoder_smiles, pad=True)

    enc_mask = enc_token_output['pad_masks']
    enc_tokens = enc_token_output['masked_tokens']
    dec_tokens = dec_token_output['original_tokens']
    dec_mask = dec_token_output['pad_masks']

    (enc_tokens, enc_mask) = check_seq_len(enc_tokens, enc_mask)
    (dec_tokens, dec_mask) = check_seq_len(dec_tokens, dec_mask)

    enc_token_ids = tokenizer.convert_tokens_to_ids(enc_tokens)
    dec_token_ids = tokenizer.convert_tokens_to_ids(dec_tokens)
    enc_token_ids = torch.tensor(enc_token_ids).transpose(0, 1)
    enc_pad_mask = torch.tensor(enc_mask,
                                dtype=torch.int64).transpose(0, 1)
    dec_token_ids = torch.tensor(dec_token_ids).transpose(0, 1)
    dec_pad_mask = torch.tensor(dec_mask,
                                dtype=torch.int64).transpose(0, 1)

    collate_output = {
        'encoder_input': enc_token_ids,
        'encoder_pad_mask': enc_pad_mask,
        'decoder_input': dec_token_ids[:-1, :],
        'decoder_pad_mask': dec_pad_mask[:-1, :],
        'target': dec_token_ids.clone()[1:, :],
        'target_pad_mask': dec_pad_mask.clone()[1:, :],
        'target_smiles': decoder_smiles,
        }

    return collate_output


class MoleculeDataset(Dataset):

    """Simple Molecule dataset that reads from a single DataFrame."""

    def __init__(self, df, split='train', zinc = False):
        """
        Args:
            df (pandas.DataFrame): DataFrame object with RDKit molecules and lengths.
        """

        if zinc:
            path = Path(data_path)
            # If path is a directory then read every subfile
            if path.is_dir():
                df = self._read_dir_df(path)
            else:
                df = pd.read_csv(path)
            self.mols = df['smiles'].tolist()
            self.lengths = None
        else:     
            self.mols = df['canonical_smiles'].tolist()
            self.lengths = df['lengths'].tolist()
        
        self.aug = SMILESRandomizer()
        val_idxs = df.index[df['set'] == 'val'].tolist()
        test_idxs = df.index[df['set'] == 'test'].tolist()
        idxs = set(range(len(df.index)))
        train_idxs = idxs - set(val_idxs).union(set(test_idxs))
        idx_map = {'train': train_idxs, 'val': val_idxs,
                   'test': test_idxs}
        self.mols = [self.mols[idx] for idx in idx_map[split]]
        if not zinc:
            self.lengths = [self.lengths[idx] for idx in idx_map[split]]

    def __len__(self):
        return len(self.mols)

    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()
        mol = self.mols[idx]
        try:
            enc_smi = self.aug(mol)
        except:
            enc_smi = mol
        try:
            dec_smi = self.aug(mol)
        except:
            dec_smi = mol
        output = {'encoder_smiles': enc_smi, 'decoder_smiles': dec_smi}
        return output

    def _read_dir_df(self, path):
        # num_cpus = 4
        # executor = ProcessPoolExecutor(num_cpus)
        # files = [f for f in path.iterdir()]
        # futures = [executor.submit(pd.read_csv, f) for f in files]
        # dfs = [future.result() for future in futures]

        dfs = [pd.read_csv(f) for f in path.iterdir()]

        zinc_df = pd.concat(dfs, ignore_index=True, copy=False)
        return zinc_df

class MoleculeDataLoader(object):

    """Loads data from a csv file containing molecules."""

    def __init__(
        self,
        file_path,
        batch_size=32,
        num_buckets=20,
        num_workers=32,
        zinc = False
        ):

        if path.is_dir():
            self.df = self._read_dir_df(path)
        else:
            self.df = pd.read_csv(path)
        #self.df = pandas.read_csv(file_path)
        train_dataset = MoleculeDataset(self.df, split='train', zinc=True)
        val_dataset = MoleculeDataset(self.df, split='val', zinc=True)
        if zinc:
            self.tokeniser = load_tokeniser(DEFAULT_VOCAB_PATH, DEFAULT_CHEM_TOKEN_START)
        else:
            self.tokenizer = \
                MolEncTokeniser.from_vocab_file(DEFAULT_VOCAB_PATH, REGEX,
                    DEFAULT_CHEM_TOKEN_START)

        world_size = \
            torch.distributed.get_world_size(group=mpu.get_data_parallel_group())
        rank = \
            torch.distributed.get_rank(group=mpu.get_data_parallel_group())
        sampler = torch.utils.data.SequentialSampler(train_dataset)
        batch_sampler = DistributedBatchSampler(sampler, batch_size,
                True, rank, world_size)

        self.train_loader = torch.utils.data.DataLoader(train_dataset,
                batch_sampler=batch_sampler, num_workers=num_workers,
                pin_memory=True, collate_fn=collate_fn)
        self.val_loader = torch.utils.data.DataLoader(val_dataset,
                num_workers=num_workers, pin_memory=True,
                collate_fn=collate_fn)

    def get_data(self):
        return (self.train_loader, self.val_loader)

    def _read_dir_df(self, path):
        # num_cpus = 4
        # executor = ProcessPoolExecutor(num_cpus)
        # files = [f for f in path.iterdir()]
        # futures = [executor.submit(pd.read_csv, f) for f in files]
        # dfs = [future.result() for future in futures]

        dfs = [pd.read_csv(f) for f in path.iterdir()]

        zinc_df = pd.concat(dfs, ignore_index=True, copy=False)
        return zinc_df