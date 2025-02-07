# File: plugin_syn_seq.py

from typing import Any, Dict, List, Optional, Union, Tuple
import pandas as pd

from pydantic import validate_arguments

from synthcity.plugins.core.plugin import Plugin
from synthcity.plugins.core.dataloader import (
    DataLoader,
    Syn_SeqDataLoader,
    create_from_info,
)
from synthcity.plugins.core.models.syn_seq.syn_seq_encoder import Syn_SeqEncoder
from synthcity.plugins.core.constraints import Constraints
from synthcity.plugins.core.distribution import constraint_to_distribution
from synthcity.plugins.core.schema import Schema
from synthcity.utils.reproducibility import enable_reproducible_results

# aggregator from syn_seq.py
from synthcity.plugins.core.models.syn_seq.syn_seq import Syn_Seq


class Syn_SeqPlugin(Plugin):
    """
    A plugin wrapping the 'Syn_Seq' aggregator in the synthcity Plugin interface.

    Steps:
      1) In .fit(), if the user passes a DataFrame, we wrap it in Syn_SeqDataLoader, then call .encode().
      2) We build or refine the domain in `_domain_rebuild`, using the original vs. converted dtype info,
         turning them into constraints, then into distributions.
      3) The aggregator trains column-by-column on the encoded data.
      4) For .generate(), we re-check constraints (including user constraints) and decode back to the original format.
    """

    @staticmethod
    def name() -> str:
        return "syn_seq"

    @staticmethod
    def type() -> str:
        return "generic"

    @staticmethod
    def hyperparameter_space(**kwargs: Any) -> List:
        # No tunable hyperparameters for demonstration
        return []

    @validate_arguments(config=dict(arbitrary_types_allowed=True))
    def __init__(
        self,
        sampling_patience: int = 100,
        strict: bool = True,
        random_state: int = 0,
        compress_dataset: bool = False,
        sampling_strategy: str = "marginal",
        **kwargs: Any
    ) -> None:
        super().__init__(
            random_state=random_state,
            sampling_patience=sampling_patience,
            strict=strict,
            compress_dataset=compress_dataset,
            sampling_strategy=sampling_strategy,
        )

        self._schema: Optional[Schema] = None
        self._training_schema: Optional[Schema] = None
        self._data_info: Optional[Dict] = None
        self._training_data_info: Optional[Dict] = {}
        self.model: Optional[Syn_Seq] = None

    @validate_arguments(config=dict(arbitrary_types_allowed=True))
    def fit(self, X: Union[DataLoader, pd.DataFrame], *args: Any, **kwargs: Any) -> Any:
        """
        Wrap a plain DataFrame into Syn_SeqDataLoader if needed, then encode the data.
        Build up the schema from original data vs. encoded data, and train the aggregator.
        """
        # If plain DataFrame, wrap in Syn_SeqDataLoader
        if isinstance(X, pd.DataFrame):
            X = Syn_SeqDataLoader(X)

        if "cond" in kwargs and kwargs["cond"] is not None:
            self.expecting_conditional = True

        enable_reproducible_results(self.random_state)
        self._data_info = X.info()

        # Build schema for the *original* data
        self._schema = Schema(
            data=X,
            sampling_strategy=self.sampling_strategy,
            random_state=self.random_state,
        )

        # Encode the data
        X_encoded, self._training_data_info = X.encode()

        # X_encoded._encoder._label_encoders => {"sex": LabelEncoder(), ...} 
        self._training_data_info["saved_label_encoders"] = X_encoded._encoder._label_encoders

        # Build an initial schema from the *encoded* data
        base_schema = Schema(
            data=X_encoded,
            sampling_strategy=self.sampling_strategy,
            random_state=self.random_state,
        )

        # Rebuild domain from original vs. converted dtype logic
        self._training_schema = self._domain_rebuild(X_encoded, base_schema)

        # aggregator training
        output = self._fit(X_encoded, *args, **kwargs)
        self.fitted = True
        return output

    def _fit(self, X: DataLoader, *args: Any, **kwargs: Any) -> "Syn_SeqPlugin":
        """
        Train the aggregator column-by-column using the encoded DataLoader.
        """
        self.model = Syn_Seq(
            random_state=self.random_state,
            strict=self.strict,
            sampling_patience=self.sampling_patience,
        )
        self.model.fit_col(X, *args, **kwargs)
        return self

    def _domain_rebuild(self, X_encoded: DataLoader, base_schema: Schema) -> Schema:
        """
        Build new domain using feature_params & constraint_to_distribution.

        For each column in the encoded data, gather basic "dtype" constraints,
        transform them into Distribution objects, and create a new schema.
        """
        enc_info = X_encoded.info()

        syn_order = enc_info.get("syn_order", [])
        orig_map = enc_info.get("original_dtype", {})
        conv_map = enc_info.get("converted_type", {})

        domain: Dict[str, Any] = {}

        # For each column in syn_order, figure out the dtype constraints
        for col in syn_order:
            col_rules = []

            original_dt = orig_map.get(col, "").lower()
            converted_dt = conv_map.get(col, "").lower()

            # Example logic (you can adapt):
            if col.endswith("_cat"):
                # definitely treat as category
                col_rules.append((col, "dtype", "category"))
            elif ("int" in original_dt or "float" in original_dt) and ("category" in converted_dt):
                col_rules.append((col, "dtype", "category"))
            elif ("object" in original_dt or "category" in original_dt) and ("numeric" in converted_dt):
                col_rules.append((col, "dtype", "float"))
            elif "date" in original_dt:
                col_rules.append((col, "dtype", "int"))
            else:
                col_rules.append((col, "dtype", "float"))

            # Build a local Constraints for this single column
            single_constraints = Constraints(rules=col_rules)
            # Then transform into a Distribution
            dist = constraint_to_distribution(single_constraints, col)
            domain[col] = dist

        # Now build new Schema with that domain
        new_schema = Schema(domain=domain)
        new_schema.sampling_strategy = base_schema.sampling_strategy
        new_schema.random_state = base_schema.random_state

        return new_schema

    @validate_arguments(config=dict(arbitrary_types_allowed=True))
    def generate(
        self,
        count: Optional[int] = None,
        constraints: Optional[Constraints] = None,
        rules: Optional[Dict[str, List[Tuple[str, str, Any]]]] = None,
        random_state: Optional[int] = None,
        **kwargs: Any,
    ) -> DataLoader:
        """
        Generate synthetic data by sampling from the aggregator, applying constraints,
        and decoding back to the original schema.
        """
        if not self.fitted:
            raise RuntimeError("Must .fit() plugin before calling .generate()")
        if self._schema is None:
            raise RuntimeError("No schema found. Fit the model first.")

        if random_state is not None:
            enable_reproducible_results(random_state)

        if count is None:
            count = self._data_info["len"]

        has_gen_cond = ("cond" in kwargs) and (kwargs["cond"] is not None)
        if has_gen_cond and not self.expecting_conditional:
            raise RuntimeError(
                "Got inference conditional, but aggregator wasn't trained with a conditional"
            )

        # Combine constraints from training schema with user constraints
        gen_constraints = self.training_schema().as_constraints()
        if constraints is not None:
            gen_constraints = gen_constraints.extend(constraints)

        # Build a schema from these constraints
        syn_schema = Schema.from_constraints(gen_constraints)

        # aggregator call
        data_syn = self._generate(count, syn_schema, rules=rules, **kwargs)

        # decode from the encoded data back to original
        data_syn = data_syn.decode()

        # # final constraints check
        # final_constraints = self.schema().as_constraints()
        # if constraints is not None:
        #     final_constraints = final_constraints.extend(constraints)

        # # If strict, keep only valid rows
        # if not data_syn.satisfies(final_constraints) and self.strict:
        #     data_syn = data_syn.match(final_constraints)

        return data_syn

    def _generate(
        self,
        count: int,
        syn_schema: Schema,
        rules: Optional[Dict[str, List[Tuple[str, str, Any]]]] = None,
        **kwargs: Any,
    ) -> DataLoader:
        """
        Internal aggregator generation logic:
        - Possibly remap rules to reference _cat columns if they specify special values
        - Let aggregator do column-by-column generation
        - Force the columns' dtypes according to syn_schema
        """
        if not self.model:
            raise RuntimeError("Aggregator not found for syn_seq plugin")

        # Remap user rules to handle special values in _cat columns
        if rules is not None:
            rules = self._remap_special_value_rules(rules, syn_schema)

        # Generate the data
        df_syn = self.model.generate_col(count, rules=rules, max_iter_rules=10)
        # Ensure correct dtypes
        df_syn = syn_schema.adapt_dtypes(df_syn)

        # create a new loader from df_syn + self._data_info
        data_syn = create_from_info(df_syn, self._data_info)

        # --- (추가) 라벨 인코더 재주입 ---
        if "saved_label_encoders" in self._training_data_info:
            data_syn._encoder._label_encoders = self._training_data_info["saved_label_encoders"]

        return data_syn

    def _remap_special_value_rules(
        self,
        rules_dict: Dict[str, List[Tuple[str, str, Any]]],
        syn_schema: Schema
    ) -> Dict[str, List[Tuple[str, str, Any]]]:
        """
        If user wrote rules referencing special values (like -0.04) on numeric columns,
        we switch them to the corresponding _cat column. This is a simple version:
        If 'val' is in 'special_value[feat_col]', rename feat_col -> feat_col_cat.
        """
        if not rules_dict:
            return rules_dict

        special_map = self._training_data_info.get("special_value", {})

        # build a base->cat map
        # e.g. if 'bp' => 'bp_cat' is in your domain
        base_to_cat = {}
        for col in syn_schema.domain:
            if col.endswith("_cat"):
                base_col = col[:-4]
                base_to_cat[base_col] = col

        new_rules = {}
        for target_col, cond_list in rules_dict.items():
            actual_target_col = target_col
            # If target_col is known to have special values => rename
            if target_col in special_map and target_col in base_to_cat:
                actual_target_col = base_to_cat[target_col]

            new_cond_list = []
            for (feat_col, op, val) in cond_list:
                new_feat = feat_col
                # If feat_col references special values => rename
                if feat_col in special_map and feat_col in base_to_cat:
                    if val in special_map[feat_col]:
                        new_feat = base_to_cat[feat_col]
                new_cond_list.append((new_feat, op, val))

            new_rules[actual_target_col] = new_cond_list

        return new_rules


# Register plugin for the library
plugin = Syn_SeqPlugin