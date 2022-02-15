"""Test :py:mod:`lmp.script.gen_txt` signatures."""

import argparse
import inspect
from inspect import Parameter, Signature
from typing import List

import lmp.script.gen_txt


def test_module_method() -> None:
  """Ensure module methods' signatures."""
  assert hasattr(lmp.script.gen_txt, 'parse_args')
  assert inspect.isfunction(lmp.script.gen_txt.parse_args)
  assert inspect.signature(lmp.script.gen_txt.parse_args) == Signature(
    parameters=[
      Parameter(
        name='argv',
        kind=Parameter.POSITIONAL_OR_KEYWORD,
        default=Parameter.empty,
        annotation=List[str],
      ),
    ],
    return_annotation=argparse.Namespace,
  )
  assert hasattr(lmp.script.gen_txt, 'main')
  assert inspect.isfunction(lmp.script.gen_txt.main)
  assert inspect.signature(lmp.script.gen_txt.main) == Signature(
    parameters=[
      Parameter(
        name='argv',
        kind=Parameter.POSITIONAL_OR_KEYWORD,
        default=Parameter.empty,
        annotation=List[str],
      ),
    ],
    return_annotation=None,
  )
