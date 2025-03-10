# stdlib
from typing import Any, Dict, List

# third party
import numpy as np
import pandas as pd
import pytest

# synthcity absolute
from synthcity.plugins.core.models.syn_seq.syn_seq import METHOD_MAP, Syn_Seq


def dummy_syn_fit(y: Any, X: Any, random_state: int = 0) -> str:
    return "dummy_model"


def dummy_generate(dummy_model: Any, Xsyn: np.ndarray) -> np.ndarray:
    return Xsyn.flatten() + 100


METHOD_MAP["cart"] = (dummy_syn_fit, dummy_generate)
METHOD_MAP["swr"] = (dummy_syn_fit, dummy_generate)


class DummyLoader:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def dataframe(self) -> pd.DataFrame:
        return self._df


class DummyLabelEncoder:
    def __init__(self, classes: List[Any]) -> None:
        self.classes_ = np.array(classes)


@pytest.fixture
def sample_df() -> pd.DataFrame:
    return pd.DataFrame({"A": [1, 2, 3, 4, 5], "B": [10, 20, 30, 40, 50]})


@pytest.fixture
def dummy_loader(sample_df: pd.DataFrame) -> DummyLoader:
    return DummyLoader(sample_df)


@pytest.fixture
def loader_info() -> Dict[str, Any]:
    return {
        "syn_order": ["A", "B"],
        "method": {"B": "cart"},
        "variable_selection": {"B": ["A"]},
    }


def test_sequential_synthesis_basic(
    dummy_loader: DummyLoader, loader_info: Dict[str, Any], sample_df: pd.DataFrame
) -> None:
    """
    Test a basic sequential synthesis:
      - Use a DataFrame with two numeric columns, A and B.
      - Define synthesis order (["A", "B"]), with column B synthesized using method "cart"
        and its predictors set as ["A"].
      - Manually simulate storing first‑column values and set a dummy fitted model for B.
      - Generate synthetic data and check that column B is generated as “A + 100.”
    """
    syn_seq = Syn_Seq(random_state=42, sampling_patience=100)
    syn_seq.fit_col(dummy_loader, label_encoder={}, loader_info=loader_info)

    assert syn_seq._syn_order == ["A", "B"]
    assert syn_seq._method_map == {"B": "cart"}
    assert syn_seq._varsel == {"B": ["A"]}

    syn_seq._first_col_values = {"A": np.array(sample_df["A"])}
    syn_seq._col_models["B"] = {"name": "cart", "fitted_model": "dummy_model"}
    syn_seq._model_trained = True

    gen_df = syn_seq.generate_col(3, label_encoder={})

    assert list(gen_df.columns) == ["A", "B"]
    assert len(gen_df) == 3
    for i in range(3):
        a_val = gen_df.loc[i, "A"]
        b_val = gen_df.loc[i, "B"]
        np.testing.assert_almost_equal(b_val, a_val + 100)


def test_sequential_synthesis_empty_data() -> None:
    """
    Test that calling fit_col on a DataLoader whose DataFrame is empty
    raises a ValueError.
    """
    empty_df = pd.DataFrame(columns=["A", "B"])
    loader = DummyLoader(empty_df)
    loader_info: Dict[str, Any] = {
        "syn_order": ["A", "B"],
        "method": {"B": "cart"},
        "variable_selection": {"B": ["A"]},
    }
    syn_seq = Syn_Seq()
    with pytest.raises(ValueError, match="No data => cannot fit Syn_Seq aggregator"):
        syn_seq.fit_col(loader, label_encoder={}, loader_info=loader_info)


@pytest.mark.parametrize("count", [0, -5])
def test_sequential_synthesis_zero_or_negative_count(count: int) -> None:
    """
    Test that calling generate_col with count <= 0 returns an empty DataFrame
    with the expected column names.
    """
    syn_seq = Syn_Seq()
    syn_seq._syn_order = ["A", "B"]
    syn_seq._model_trained = True
    gen_df = syn_seq.generate_col(count, label_encoder={})
    assert gen_df.empty
    assert list(gen_df.columns) == ["A", "B"]


def test_sequential_synthesis_not_fitted() -> None:
    """
    Test that calling generate_col before the model is marked as trained
    raises a RuntimeError.
    """
    syn_seq = Syn_Seq()
    with pytest.raises(RuntimeError, match="Syn_Seq aggregator not yet fitted"):
        syn_seq.generate_col(3, label_encoder={})
