# syn_seq_preprocess.py

import pandas as pd
import numpy as np
from typing import Optional, Dict, List, Any


class SynSeqPreprocessor:
    """
    전처리(preprocess) & 후처리(postprocess) 클래스를 함수화하여 단계별로 깔끔하게 정리.

    - max_categories 로직을 넣어 user_dtypes에 없는 컬럼은 auto로 category/numeric 판단
    - 날짜(col_type == "date")이면 to_datetime
    - 범주형(col_type == "category")이면 astype('category')
    - numeric + special value -> (base_col, base_col_cat) 분리
    """

    def __init__(
        self,
        user_dtypes: Optional[Dict[str, str]] = None,
        user_special_values: Optional[Dict[str, List[Any]]] = None,
        max_categories: int = 20,
    ):
        """
        Args:
            user_dtypes: {col: "date"/"category"/"numeric"} 등. (없으면 auto 결정)
            user_special_values: {col: [특수값1, 특수값2, ...]}
            max_categories: auto 판단 시, nunique <= max_categories 이면 category, else numeric
        """
        self.user_dtypes = user_dtypes or {}
        self.user_special_values = user_special_values or {}
        self.max_categories = max_categories

        # 내부 저장용
        self.original_dtypes: Dict[str, str] = {}  # {col: original_dtype}
        self.split_map: Dict[str, str] = {}        # {base_col -> cat_col}
        self.detected_specials: Dict[str, List[Any]] = {}  # user special values

    # =========================================================================
    # PREPROCESS
    # =========================================================================
    def preprocess(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        1) 원본 dtype 기록
        2) user_dtypes or auto 판단 -> date/category/numeric 세팅
        3) numeric + special_value -> split
        """
        df = df.copy()

        # (a) 원본 dtype 저장
        self._record_original_dtypes(df)

        # (b) user_dtypes 없는 컬럼은 auto -> category/numeric
        self._auto_assign_dtypes(df)

        # (c) user_dtypes 적용: date->datetime, category->astype('category'), numeric->그대로
        self._apply_user_dtypes(df)

        # (d) numeric + special_value split
        self._split_numeric_columns(df)

        return df

    def _record_original_dtypes(self, df: pd.DataFrame):
        for col in df.columns:
            self.original_dtypes[col] = str(df[col].dtype)

    def _auto_assign_dtypes(self, df: pd.DataFrame):
        """
        user_dtypes에 명시가 없으면,
         - nuniq <= max_categories -> 'category'
         - else 'numeric'
         - 만약 datetime64 타입이면 'date'로 지정
        """
        for col in df.columns:
            if col in self.user_dtypes:
                continue

            # datetime 타입?
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                self.user_dtypes[col] = "date"
                print(f"[auto_assign] {col} -> date")
                continue

            nuniq = df[col].nunique(dropna=False)
            if nuniq <= self.max_categories:
                self.user_dtypes[col] = "category"
                print(f"[auto_assign] {col} -> category (nuniq={nuniq})")
            else:
                self.user_dtypes[col] = "numeric"
                print(f"[auto_assign] {col} -> numeric (nuniq={nuniq})")

    def _apply_user_dtypes(self, df: pd.DataFrame):
        """
        1) date -> pd.to_datetime
        2) category -> astype('category')
        3) numeric -> 그대로
        """
        for col, dtype_str in self.user_dtypes.items():
            if col not in df.columns:
                continue

            if dtype_str == "date":
                df[col] = pd.to_datetime(df[col], errors="coerce")
            elif dtype_str == "category":
                df[col] = df[col].astype("category")
            else:
                # 'numeric' 등은 그대로
                pass

    def _split_numeric_columns(self, df: pd.DataFrame):
        """
        user_special_values가 있는 numeric 컬럼만 분할:
         - base_col -> special -> NaN
         - cat_col -> special -> 문자열, 그 외 -> '-777'
         -> cat_col을 base_col 직전에 insert
        """
        for col, specials in self.user_special_values.items():
            # user_special_values에 있다면 numeric으로 가정
            # (user_dtypes[col]=='numeric') 로 확인해도 됨
            if col not in df.columns:
                continue

            cat_col = col + "_cat"
            self.split_map[col] = cat_col
            self.detected_specials[col] = specials

            def cat_mapper(v):
                if pd.isna(v):
                    return "-9999"
                return str(v) if v in specials else "-777"

            df[cat_col] = df[col].apply(cat_mapper).astype("category")

            def base_mapper(v):
                if v in specials:
                    return np.nan
                return v
            df[col] = df[col].apply(base_mapper)

            if cat_col in df.columns:
                df.drop(columns=[cat_col], inplace=True)
            base_idx = df.columns.get_loc(col)
            df.insert(base_idx, cat_col, None)
            df[cat_col] = df[col].apply(cat_mapper).astype("category")

    # =========================================================================
    # POSTPROCESS
    # =========================================================================
    def postprocess(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        split된 (base_col, cat_col)을 합쳐서 특수값 복원.
        (date offset 변환은 없음)
        """
        df = df.copy()
        df = self._merge_splitted_cols(df)
        return df

    def _merge_splitted_cols(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        split_map: {base_col->cat_col}
        -> base_col이 NaN이고, cat_col 값이 specials면 복원
        -> cat_col drop
        """
        for base_col, cat_col in self.split_map.items():
            if base_col not in df.columns or cat_col not in df.columns:
                continue

            specials = self.detected_specials.get(base_col, [])

            for i in range(len(df)):
                if pd.isna(df.at[i, base_col]):
                    cat_val = df.at[i, cat_col]
                    try:
                        possible_val = float(cat_val)
                    except:
                        possible_val = cat_val

                    if possible_val in specials:
                        df.at[i, base_col] = possible_val
                    else:
                        # -9999 => NaN 유지, -777 => numeric
                        pass

            df.drop(columns=[cat_col], inplace=True)

        return df
