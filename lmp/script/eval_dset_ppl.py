r"""Use pre-trained language model checkpoints to calculate average perplexity on a dataset.

One must first run the script :py:mod:`lmp.script.train_model` before running this script.
Use ``pipenv run tensorboard`` to launch tensorboard and open browser with URL http://localhost:6006/ to see evaluation
results over selected model checkpoints.

See Also
--------
:doc:`lmp.model </model/index>`
  All available language models.
:doc:`lmp.script.eval_txt_ppl </script/eval_txt_ppl>`
  Use pre-trained language model to calculate perplexity on given text.
:doc:`lmp.script.train_model </script/train_model>`
  Train language model.

Examples
--------
The following example evaluate language model experiment ``my_model_exp`` on :py:class:`lmp.dset.WikiText2` dataset
with version ``valid``.
It evaluates checkpoints whose numbers are larger than or equal to ``5000``.

.. code-block::

  python -m lmp.script.eval_dset_ppl wiki-text-2 \
    --batch_size 32 \
    --first_ckpt 5000 \
    --exp_name my_model_exp \
    --ver valid

The following example only evaluate on the last checkpoint.

.. code-block::

  python -m lmp.script.eval_dset_ppl wiki-text-2 \
    --batch_size 32 \
    --first_ckpt -1 \
    --exp_name my_model_exp \
    --ver valid

There are many checkpoints to be evaluated.
One can specify checkpoint range one want to evaluate.

.. code-block::

  python -m lmp.script.eval_dset_ppl wiki-text-2 \
    --batch_size 32 \
    --first_ckpt 5000 \
    --exp_name my_model_exp \
    --last_ckpt 10000 \
    --ver valid

Since evaluation do not need to construct tensor graph when perform forward pass, model will consume less memory than
training.
Thus we can use larger batch size to accelerate evaluation process.

.. code-block::

  python -m lmp.script.eval_dset_ppl wiki-text-2 \
    --batch_size 128 \
    --ckpt -1 \
    --exp_name my_model_exp \
    --ver valid

You can use ``-h`` or ``--help`` options to get a list of supported CLI arguments.

.. code-block:: shell

  python -m lmp.script.eval_dset_ppl -h
"""

import argparse
import gc
import sys
from typing import List

import torch
import torch.utils.data
# Typeshed for `tqdm` is not available, we ignore type check on `tqdm`.
from tqdm import tqdm  # type: ignore

import lmp.dset
import lmp.model
import lmp.util.cfg
import lmp.util.dset
import lmp.util.log
import lmp.util.metric
import lmp.util.model
import lmp.util.rand
import lmp.util.tknzr
import lmp.util.validate
from lmp.tknzr._base import PAD_TKID


def parse_args(argv: List[str]) -> argparse.Namespace:
  """Parse CLI arguments.

  Parameters
  ----------
  argv: list[str]
    List of CLI arguments.

  See Also
  --------
  sys.argv
    Python CLI arguments interface.

  Returns
  -------
  argparse.Namespace
    Parsed CLI arguments.
  """
  # Create parser.
  parser = argparse.ArgumentParser(
    'python -m lmp.script.eval_dset_ppl',
    description='Use pre-trained language model checkpoints to calculate average perplexity on a particular dataset.',
  )

  # Use dataset name to create subparser for all datasets.
  subparsers = parser.add_subparsers(dest='dset_name', required=True)
  for dset_name, dset_type in lmp.dset.DSET_OPTS.items():
    dset_subparser = subparsers.add_parser(
      dset_name,
      description=f'Calculate perplexity on {dset_type.__name__} dataset.',
    )

    # Required arguments.
    dset_subparser.add_argument(
      '--batch_size',
      help='Evaluation mini-batch size.',
      required=True,
      type=int,
    )
    dset_subparser.add_argument(
      '--exp_name',
      help='Pre-trained language model experiment name.',
      required=True,
      type=str,
    )
    dset_subparser.add_argument(
      '--first_ckpt',
      help='The first checkpoint of pre-trained language model to be evaluated.',
      required=True,
      type=int,
    )

    # Optional arguments.
    dset_subparser.add_argument(
      '--last_ckpt',
      default=-1,
      help='The last checkpoint of pre-trained language model to be evaluated.  Default is ``-1``.',
      type=int,
    )
    dset_subparser.add_argument(
      '--seed',
      default=42,
      help='Random seed.  Default is ``42``',
      type=int,
    )
    dset_subparser.add_argument(
      '--ver',
      default=None,
      help=f'Version of the {dset_type.__name__} dataset.  Defaults to {dset_type.df_ver}.',
      choices=dset_type.vers,
      type=str,
    )

  return parser.parse_args(argv)


def main(argv: List[str]) -> None:
  """Script entry point.

  Parameters
  ----------
  argv: list[str]
    List of CLI arguments.

  Returns
  -------
  None
  """
  # Parse CLI arguments.
  args = parse_args(argv=argv)

  # `args.batch_size` validation.
  lmp.util.validate.raise_if_wrong_ordered(vals=[1, args.batch_size], val_names=['1', 'args.batch_size'])
  # `args.first_ckpt` validation.
  lmp.util.validate.raise_if_wrong_ordered(vals=[-1, args.first_ckpt], val_names=['-1', 'args.first_ckpt'])
  # `args.last_ckpt` validation.
  lmp.util.validate.raise_if_wrong_ordered(vals=[-1, args.last_ckpt], val_names=['-1', 'args.last_ckpt'])

  # Set random seed for reproducibility.
  lmp.util.rand.set_seed(seed=args.seed)

  # Get model running device.
  device = torch.device('cpu')
  if torch.cuda.is_available():
    device = torch.device('cuda')

  # Load pre-trained model configuration.
  model_cfg = lmp.util.cfg.load(exp_name=args.exp_name)

  # Load pre-trained tokenizer instance.
  tknzr = lmp.util.tknzr.load(exp_name=model_cfg.tknzr_exp_name)

  # Get dataset instance of specific version.
  dset = lmp.util.dset.load(**args.__dict__)
  dset_size = len(dset)

  # Mini-batch sampler.
  data_loader = torch.utils.data.DataLoader(
    batch_size=args.batch_size,
    dataset=dset,
    shuffle=False,
  )

  # Get tensorboard logger instance.
  writer = lmp.util.log.get_tb_logger(exp_name=args.exp_name)

  # Evaluate checkpoints within ranges.
  best_ckpt = -1
  best_avg_ppl = 0.0
  for ckpt in lmp.util.model.list_ckpts(exp_name=args.exp_name, first_ckpt=args.first_ckpt, last_ckpt=args.last_ckpt):
    # Load pre-trained model instance.
    model = lmp.util.model.load(ckpt=ckpt, exp_name=args.exp_name)

    # Set model to evaluation mode.
    # This turn off dropout layers in model.
    model = model.eval()

    # Move model to running device.
    model = model.to(device)

    # Record average perplexity.
    avg_ppl = 0.0
    for batch_txt in tqdm(data_loader):
      # Encode batch text into batch token ids.
      # We convert batch token ids into tensor and move to tensor to the same running device as model.
      batch_tkids = torch.LongTensor(tknzr.batch_enc(batch_txt=batch_txt, max_seq_len=model_cfg.max_seq_len)).to(device)

      # Loop through mini-batch by context windows.
      batch_prev_states = None
      batch_tkids_pd_list = []
      batch_next_tkids_list = []
      for ctx_idx in range(0, model_cfg.max_seq_len, model_cfg.ctx_win):
        # Fetch context window.
        ctx_batch_tkids = batch_tkids[..., ctx_idx:ctx_idx + model_cfg.ctx_win + 1]

        # Drop the remaining sequence-length-1 context window.
        if ctx_batch_tkids.size(1) == 1:
          break

        # Skip all-paddings batch.
        if torch.all(ctx_batch_tkids == PAD_TKID):
          break

        # Construct language model evaluation format.
        batch_cur_tkids = ctx_batch_tkids[..., :-1]
        batch_next_tkids = ctx_batch_tkids[..., 1:]

        # Get next token id probability distribution.
        batch_tkids_pd, batch_prev_states = model.pred(
          batch_cur_tkids=batch_cur_tkids,
          batch_prev_states=None,
        )

        batch_next_tkids_list.append(batch_next_tkids)
        batch_tkids_pd_list.append(batch_tkids_pd)

      # Calculate perplexity.
      batch_ppl = lmp.util.metric.ppl(
        batch_tkids=torch.cat(batch_next_tkids_list, dim=1),
        batch_tkids_pd=torch.cat(batch_tkids_pd_list, dim=1),
      )

      # Accumulate average perplexity.
      avg_ppl += (batch_ppl / dset_size).sum().item()

    # Log average perplexity on dataset to CLI and tensorboard.
    writer.add_scalar(f'ppl/{args.dset_name}/{args.ver}', avg_ppl, ckpt)
    print(f'checkpoint: {ckpt}, avg ppl: {avg_ppl}')

    if best_avg_ppl > avg_ppl or best_ckpt == -1:
      best_ckpt = ckpt
      best_avg_ppl = avg_ppl

  print(f'best checkpoint: {best_ckpt}, best avg ppl: {best_avg_ppl}')

  # Free memory.
  # This is only need for unit test.
  del args
  del avg_ppl
  del batch_cur_tkids
  del batch_next_tkids
  del batch_next_tkids_list
  del batch_ppl
  del batch_prev_states
  del batch_tkids
  del batch_tkids_pd
  del batch_tkids_pd_list
  del best_avg_ppl
  del best_ckpt
  del ckpt
  del ctx_batch_tkids
  del ctx_idx
  del data_loader
  del device
  del dset
  del dset_size
  del model
  del model_cfg
  del tknzr
  del writer
  torch.cuda.empty_cache()
  gc.collect()


if __name__ == '__main__':
  main(argv=sys.argv[1:])
