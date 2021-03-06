import torch
import multiprocessing
import pytorch_lightning as pl
from rdkit import Chem
from functools import partial
from typing import List, Optional
from torch.utils.data import DataLoader
from pysmilesutils.augment import MolRandomizer

from molbart.tokeniser import MolEncTokeniser
from molbart.data.util import TokenSampler
from molbart.data.datasets import MoleculeDataset, ReactionDataset


class _AbsDataModule(pl.LightningDataModule):
    def __init__(
        self,
        dataset,
        tokeniser,
        batch_size,
        max_seq_len,
        train_token_batch_size=None,
        num_buckets=None,
        val_idxs=None, 
        test_idxs=None,
        split_perc=0.2
    ):
        super(_AbsDataModule, self).__init__()

        if val_idxs is not None and test_idxs is not None:
            idxs_intersect = set(val_idxs).intersection(set(test_idxs))
            if len(idxs_intersect) > 0:
                raise ValueError(f"Val idxs and test idxs overlap")

        if train_token_batch_size is not None and num_buckets is not None:
            print(f"""Training with approx. {train_token_batch_size} tokens per batch"""
                f""" and {num_buckets} buckets in the sampler.""")
        else:
            print(f"Using a batch size of {batch_size} for training.")

        self.dataset = dataset
        self.tokeniser = tokeniser

        self.batch_size = batch_size
        self.max_seq_len = max_seq_len
        self.train_token_batch_size = train_token_batch_size
        self.num_buckets = num_buckets
        self.val_idxs = val_idxs
        self.test_idxs = test_idxs
        self.split_perc = split_perc

        self._num_workers = multiprocessing.cpu_count()

        self.train_dataset = None
        self.val_dataset = None
        self.test_dataset = None

    # Use train_token_batch_size with TokenSampler for training and batch_size for validation and testing
    def train_dataloader(self):
        if self.train_token_batch_size is None:
            loader = DataLoader(
                self.train_dataset, 
                batch_size=self.batch_size,
                num_workers=self._num_workers, 
                collate_fn=self._collate,
                shuffle=True
            )
            return loader

        sampler = TokenSampler(
            self.num_buckets,
            self.train_dataset.seq_lengths,
            self.train_token_batch_size,
            shuffle=True
        )
        loader = DataLoader(
            self.train_dataset,
            batch_sampler=sampler,
            num_workers=self._num_workers,
            collate_fn=self._collate
        )
        return loader

    def val_dataloader(self):
        loader = DataLoader(
            self.val_dataset, 
            batch_size=self.batch_size,
            num_workers=self._num_workers, 
            collate_fn=partial(self._collate, train=False)
        )
        return loader

    def test_dataloader(self):
        loader = DataLoader(
            self.test_dataset, 
            batch_size=self.batch_size,
            num_workers=self._num_workers, 
            collate_fn=partial(self._collate, train=False)
        )
        return loader

    def setup(self, stage=None):
        train_dataset = None
        val_dataset = None
        test_dataset = None

        # Split datasets by idxs passed in...
        if self.val_idxs is not None and self.test_idxs is not None:
            train_dataset, val_dataset, test_dataset = self.dataset.split_idxs(self.val_idxs, self.test_idxs)

        # Or randomly
        else:
            train_dataset, val_dataset, test_dataset = self.dataset.split(self.split_perc, self.split_perc)

        self.train_dataset = train_dataset
        self.val_dataset = val_dataset
        self.test_dataset = test_dataset

    def _collate(self, batch, train=True):
        raise NotImplementedError()

    def _check_seq_len(self, tokens, mask):
        """ Warn user and shorten sequence if the tokens are too long, otherwise return original

        Args:
            tokens (List[List[str]]): List of token sequences
            mask (List[List[int]]): List of mask sequences

        Returns:
            tokens (List[List[str]]): List of token sequences (shortened, if necessary)
            mask (List[List[int]]): List of mask sequences (shortened, if necessary)
        """

        seq_len = max([len(ts) for ts in tokens])
        if seq_len > self.max_seq_len:
            print(f"WARNING -- Sequence length {seq_len} is larger than maximum sequence size")

            tokens_short = [ts[:self.max_seq_len] for ts in tokens]
            mask_short = [ms[:self.max_seq_len] for ms in mask]

            return tokens_short, mask_short

        return tokens, mask


class MoleculeDataModule(_AbsDataModule):
    def __init__(
        self,
        dataset: MoleculeDataset,
        tokeniser: MolEncTokeniser,
        batch_size: int,
        max_seq_len: int,
        train_token_batch_size: Optional[int] = None,
        num_buckets: Optional[int] = None,
        val_idxs: Optional[List[int]] = None, 
        test_idxs: Optional[List[int]] = None,
        split_perc: Optional[float] = 0.2,
        augment: Optional[bool] = True 
    ):
        super(MoleculeDataModule, self).__init__(
            dataset,
            tokeniser,
            batch_size,
            max_seq_len,
            train_token_batch_size,
            num_buckets,
            val_idxs, 
            test_idxs,
            split_perc
        )

        if augment:
            print("Using molecule data module with augmentations.")
            self.aug = MolRandomizer() 
        else:
            print("No molecular augmentation.")
            self.aug = None

    def _collate(self, batch, train=True):
        token_output = self._prepare_tokens(batch, train)
        enc_tokens = token_output["encoder_tokens"]
        enc_pad_mask = token_output["encoder_pad_mask"]
        dec_tokens = token_output["decoder_tokens"]
        dec_pad_mask = token_output["decoder_pad_mask"]
        target_smiles = token_output["target_smiles"]

        enc_token_ids = self.tokeniser.convert_tokens_to_ids(enc_tokens)
        dec_token_ids = self.tokeniser.convert_tokens_to_ids(dec_tokens)

        enc_token_ids = torch.tensor(enc_token_ids).transpose(0, 1)
        enc_pad_mask = torch.tensor(enc_pad_mask, dtype=torch.bool).transpose(0, 1)
        dec_token_ids = torch.tensor(dec_token_ids).transpose(0, 1)
        dec_pad_mask = torch.tensor(dec_pad_mask, dtype=torch.bool).transpose(0, 1)

        collate_output = {
            "encoder_input": enc_token_ids,
            "encoder_pad_mask": enc_pad_mask,
            "decoder_input": dec_token_ids[:-1, :],
            "decoder_pad_mask": dec_pad_mask[:-1, :],
            "target": dec_token_ids.clone()[1:, :],
            "target_pad_mask": dec_pad_mask.clone()[1:, :],
            "target_smiles": target_smiles
        }

        return collate_output

    def _prepare_tokens(self, batch, train):
        aug = self.aug is not None
        if aug:
            encoder_mols = self.aug(batch)
            decoder_mols = self.aug(batch)
        else:
            encoder_mols = batch[:]
            decoder_mols = batch[:]

        canonical = self.aug is None
        enc_smiles = []
        dec_smiles = []

        # There is a very rare possibility that RDKit will not be able to generate the SMILES for the augmented mol
        # In this case we just use the canonical mol to generate the SMILES
        for idx, (enc_mol, dec_mol) in enumerate(zip(encoder_mols, decoder_mols)):
            try:
                enc_smi = Chem.MolToSmiles(enc_mol, canonical=canonical)
            except RuntimeError:
                enc_smi = Chem.MolToSmiles(batch[idx], canonical=True)
                print(f"Could not generate smiles after augmenting: {enc_smi}")

            try:
                dec_smi = Chem.MolToSmiles(dec_mol, canonical=canonical)
            except RuntimeError:
                dec_smi = Chem.MolToSmiles(batch[idx], canonical=True)
                print(f"Could not generate smiles after augmenting: {dec_smi}")

            enc_smiles.append(enc_smi)
            dec_smiles.append(dec_smi)

        enc_token_output = self.tokeniser.tokenise(enc_smiles, mask=True, pad=True)
        dec_token_output = self.tokeniser.tokenise(dec_smiles, pad=True)

        enc_mask = enc_token_output["pad_masks"]
        if train:
            enc_tokens = enc_token_output["masked_tokens"]
        else:
            enc_tokens = enc_token_output["original_tokens"]

        dec_tokens = dec_token_output["original_tokens"]
        dec_mask = dec_token_output["pad_masks"]

        enc_tokens, enc_mask = self._check_seq_len(enc_tokens, enc_mask)
        dec_tokens, dec_mask = self._check_seq_len(dec_tokens, dec_mask)

        token_output = {
            "encoder_tokens": enc_tokens,
            "encoder_pad_mask": enc_mask,
            "decoder_tokens": dec_tokens,
            "decoder_pad_mask": dec_mask,
            "target_smiles": dec_smiles
        }

        return token_output


class FineTuneReactionDataModule(_AbsDataModule):
    def __init__(
        self,
        dataset: ReactionDataset,
        tokeniser: MolEncTokeniser,
        batch_size: int,
        max_seq_len: int,
        train_token_batch_size: Optional[int] = None,
        num_buckets: Optional[int] = None,
        forward_pred: Optional[bool] = True,
        val_idxs: Optional[List[int]] = None, 
        test_idxs: Optional[List[int]] = None,
        split_perc: Optional[float] = 0.2,
        augment: Optional[str] = None
    ):
        super().__init__(
            dataset,
            tokeniser,
            batch_size,
            max_seq_len,
            train_token_batch_size,
            num_buckets,
            val_idxs, 
            test_idxs,
            split_perc
        )

        if augment is None:
            print("No data augmentation.")
        elif augment == "reactants":
            print("Augmenting reactants only.")
        elif augment == "all":
            print("Augmenting both reactants and products.")
        else:
            raise ValueError(f"Unknown value for augment, {augment}")

        self.augment = augment
        self.aug = MolRandomizer() if augment is not None else None
        self.forward_pred = forward_pred

    def _collate(self, batch, train=True):
        # TODO Allow both forward and backward prediction

        token_output = self._prepare_tokens(batch, train)
        reacts_tokens = token_output["reacts_tokens"]
        reacts_mask = token_output["reacts_mask"]
        prods_tokens = token_output["prods_tokens"]
        prods_mask = token_output["prods_mask"]
        prods_smiles = token_output["products_smiles"]

        reacts_token_ids = self.tokeniser.convert_tokens_to_ids(reacts_tokens)
        prods_token_ids = self.tokeniser.convert_tokens_to_ids(prods_tokens)

        reacts_token_ids = torch.tensor(reacts_token_ids).transpose(0, 1)
        reacts_pad_mask = torch.tensor(reacts_mask, dtype=torch.bool).transpose(0, 1)
        prods_token_ids = torch.tensor(prods_token_ids).transpose(0, 1)
        prods_pad_mask = torch.tensor(prods_mask, dtype=torch.bool).transpose(0, 1)

        collate_output = {
            "encoder_input": reacts_token_ids,
            "encoder_pad_mask": reacts_pad_mask,
            "decoder_input": prods_token_ids[:-1, :],
            "decoder_pad_mask": prods_pad_mask[:-1, :],
            "target": prods_token_ids.clone()[1:, :],
            "target_pad_mask": prods_pad_mask.clone()[1:, :],
            "target_smiles": prods_smiles
        }

        return collate_output

    def _prepare_tokens(self, batch, train):
        """ Prepare smiles strings for the model

        RDKit is used to construct a smiles string
        The smiles strings are prepared for the forward prediction task, no masking

        Args:
            batch (list[tuple(Chem.Mol, Chem.Mol)]): Batched input to the model
            train (bool): Whether generating data for training or not

        Output:
            Dictionary output from tokeniser: {
                "reacts_tokens" (List[List[str]]): Reactant tokens from tokeniser,
                "prods_tokens" (List[List[str]]): Product tokens from tokeniser,
                "reacts_masks" (List[List[int]]): 0 refers to not padded, 1 refers to padded,
                "prods_masks" (List[List[int]]): 0 refers to not padded, 1 refers to padded
            }
        """

        reacts, prods = tuple(zip(*batch))

        if self.augment == "reactants" or self.augment == "all":
            reacts = [Chem.MolToSmiles(react, canonical=False) for react in self.aug(reacts)]
        else:
            reacts = [Chem.MolToSmiles(react) for react in reacts]

        if self.augment == "all":
            prods = [Chem.MolToSmiles(prod, canonical=False) for prod in self.aug(prods)]
        else:
            prods = [Chem.MolToSmiles(prod) for prod in prods]

        reacts_output = self.tokeniser.tokenise(reacts, pad=True)
        prods_output = self.tokeniser.tokenise(prods, pad=True)

        reacts_tokens = reacts_output["original_tokens"]
        reacts_mask = reacts_output["pad_masks"]
        reacts_tokens, reacts_mask = self._check_seq_len(reacts_tokens, reacts_mask)

        prods_tokens = prods_output["original_tokens"]
        prods_mask = prods_output["pad_masks"]
        prods_tokens, prods_mask = self._check_seq_len(prods_tokens, prods_mask)

        token_output = {
            "reacts_tokens": reacts_tokens,
            "reacts_mask": reacts_mask,
            "prods_tokens": prods_tokens,
            "prods_mask": prods_mask,
            "reactants_smiles": reacts,
            "products_smiles": prods
        }

        return token_output


class FineTuneMolOptDataModule(_AbsDataModule):
    def __init__(
        self,
        dataset: ReactionDataset,
        tokeniser: MolEncTokeniser,
        batch_size: int,
        max_seq_len: int,
        val_idxs: Optional[List[int]] = None, 
        test_idxs: Optional[List[int]] = None,
        split_perc: Optional[float] = 0.2
    ):
        super().__init__(
            dataset,
            tokeniser,
            batch_size,
            max_seq_len,
            val_idxs, 
            test_idxs,
            split_perc
        )

    def _collate(self, batch, train=True):
        token_output = self._prepare_tokens(batch, train)
        reacts_tokens = token_output["reacts_tokens"]
        reacts_mask = token_output["reacts_mask"]
        prods_tokens = token_output["prods_tokens"]
        prods_mask = token_output["prods_mask"]
        prods_smiles = token_output["products_smiles"]

        reacts_token_ids = self.tokeniser.convert_tokens_to_ids(reacts_tokens)
        prods_token_ids = self.tokeniser.convert_tokens_to_ids(prods_tokens)

        reacts_token_ids = torch.tensor(reacts_token_ids).transpose(0, 1)
        reacts_pad_mask = torch.tensor(reacts_mask, dtype=torch.bool).transpose(0, 1)
        prods_token_ids = torch.tensor(prods_token_ids).transpose(0, 1)
        prods_pad_mask = torch.tensor(prods_mask, dtype=torch.bool).transpose(0, 1)

        collate_output = {
            "encoder_input": reacts_token_ids,
            "encoder_pad_mask": reacts_pad_mask,
            "decoder_input": prods_token_ids[:-1, :],
            "decoder_pad_mask": prods_pad_mask[:-1, :],
            "target": prods_token_ids.clone()[1:, :],
            "target_pad_mask": prods_pad_mask.clone()[1:, :],
            "target_smiles": prods_smiles
        }

        return collate_output

    def _prepare_tokens(self, batch, train):
        # TODO

        pass
