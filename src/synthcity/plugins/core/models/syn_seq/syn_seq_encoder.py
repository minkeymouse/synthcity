from typing import Optional, Dict, List, Any
import pandas as pd
import numpy as np
from sklearn.base import TransformerMixin, BaseEstimator
from sklearn.preprocessing import LabelEncoder


class Syn_SeqEncoder(TransformerMixin, BaseEstimator):
    """
    syn_seq용 column-by-column 인코더.
      - fit: 전처리 준비 (col_map, variable_selection 설정 등)
      - transform: 실제 변환 (date->offset, numeric->split, dtype 적용, etc.)
      - get_info: 고정된 키 이름으로 딕트 반환
        => "syn_order", "original_dtype", "converted_type", "method", 
           "special_value"(dict형), "date_mins", "variable_selection"
    """

    def __init__(
        self,
        special_value: Optional[Dict[str, List]] = None,  # 기존 columns_special_values => special_value 로 네이밍 변경
        syn_order: Optional[List[str]] = None,
        method: Optional[Dict[str, str]] = None,
        max_categories: int = 20,
        col_type: Optional[Dict[str, str]] = None,
        variable_selection: Optional[Dict[str, List]] = None,
        default_method: str = "cart",
    ):
        """
        Args:
            special_value: 예) {"bp":[-0.04, -0.01]} + auto로 감지된 freq>0.9 값도 합쳐 저장
            syn_order: 순차적 모델링 순서
            method: 유저 오버라이드 {"bp":"norm", ...}
            max_categories: 범주 vs 수치 구분 임계값
            col_type: {"age":"category","some_date":"date"} 등 명시
            variable_selection: { col: [predictors...] }
            default_method: 첫 컬럼 제외 기본값 "cart" (첫 컬럼은 없으면 "swr")
        """
        self.syn_order = syn_order or []
        self.method = method or {}
        self.max_categories = max_categories
        self.col_type = col_type or {}
        self.variable_selection_ = variable_selection or {}
        self.default_method = default_method

        # special_value: col -> list of special vals
        self.special_value: Dict[str, List] = special_value or {}

        # col_map: {col: {"original_dtype":"float64","converted_type":"numeric","method":"cart"}, ...}
        self.col_map: Dict[str, Dict[str, Any]] = {}
        self.date_mins: Dict[str, pd.Timestamp] = {}

        self._label_encoders = {}
        # self._is_fit = False

    def fit(self, X: pd.DataFrame) -> "Syn_SeqEncoder":
        X = X.copy()
        # 1) syn_order 정리
        self._detect_syn_order(X)
        # 2) col_map 초기화
        self._init_col_map(X)
        # 3) special value 감지(유저+freq>0.9) → self.special_value에 통합
        self._detect_special_values(X)
        # 4) date min 기록
        self._store_date_min(X)
        # 5) method 할당
        self._assign_method_to_cols()
        # 6) variable_selection 세팅(유저 없으면 기본)
        self._assign_variable_selection()

        # self._is_fit = True
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        # if not self._is_fit:
        #     raise RuntimeError("Must fit() first.")

        X = X.copy()
        X = self._reorder_columns(X)
        X = self._convert_date_to_offset(X)
        X = self._split_numeric_cols_in_front(X)  # special_value에 맞춰 _cat 생성
        X = self._apply_converted_dtype(X)
        self._update_varsel_dict_after_split(X)
        # 6) (중요) 'converted_type' == "category" 인 컬럼 전부 Label Encoding
        for col in self.syn_order:
            # col_map에 저장된 converted_type이 category인 컬럼만 대상
            cinfo = self.col_map[col]
            if col in X.columns and cinfo["converted_type"] == "category":
                
                # (a) 아직 해당 컬럼에 LabelEncoder가 없으면 여기서 fit
                if col not in self._label_encoders:
                    le = LabelEncoder()
                    series_for_fit = X[col].astype(str).fillna("NAN")
                    le.fit(series_for_fit)
                    self._label_encoders[col] = le
                
                # (b) 실제 transform(문자열 -> 정수)  
                le = self._label_encoders[col]
                X[col] = X[col].astype(str).fillna("NAN")
                X[col] = le.transform(X[col])
        
        return X

    def inverse_transform(self, X: pd.DataFrame) -> pd.DataFrame:
        """
        1) 날짜 offset -> date 복원
        2) 분할된 _cat 컬럼들 다시 base 컬럼과 merge하여 special values 복원
        3) 원본 dtype으로 캐스팅
        """
        X = X.copy()
        # 0) Label Decoding
        #    self._label_encoders 딕셔너리에 등록된(=transform에서 인코딩했던) 컬럼만 복원
        cat_cols = [col for col in X.columns if col in self._label_encoders]
        for col in cat_cols:
            le = self._label_encoders[col]
            # transform()에서 int 코드로 바꿨으므로 astype(int) 후 decode
            X[col] = X[col].astype(int)
            X[col] = le.inverse_transform(X[col])
            
        # 1) date offset -> date
        X = self._convert_offset_to_date(X)

        # 2) split했던 numeric cols + _cat 을 재합치는 단계
        X = self._merge_splitted_cols(X)

        # 3) 원본 dtype 복원
        for col in X.columns:
            if col in self.col_map:
                orig_dt = self.col_map[col].get("original_dtype")
                if orig_dt:
                    try:
                        X[col] = X[col].astype(orig_dt)
                    except:
                        pass
        return X

    # ---------------------- (fit) helpers ----------------------
    def _detect_syn_order(self, X: pd.DataFrame):
        if not self.syn_order:
            self.syn_order = list(X.columns)
        else:
            self.syn_order = [c for c in self.syn_order if c in X.columns]

    def _init_col_map(self, X: pd.DataFrame):
        self.col_map.clear()
        for col in self.syn_order:
            if col not in X.columns:
                continue
            orig_dt = str(X[col].dtype)

            declared = self.col_type.get(col, "").lower()
            if declared == "date":
                conv_type = "numeric"
            elif declared == "numeric":
                conv_type = "numeric"
            elif declared == "category":
                conv_type = "category"
            else:
                # auto
                if pd.api.types.is_datetime64_any_dtype(X[col]):
                    conv_type = "numeric"
                    self.col_type[col] = "date"
                else:
                    nuniq = X[col].nunique(dropna=False)
                    if nuniq <= self.max_categories:
                        conv_type = "category"
                    else:
                        conv_type = "numeric"

            self.col_map[col] = {
                "original_dtype": orig_dt,
                "converted_type": conv_type,
                "method": None
            }

    def _detect_special_values(self, X: pd.DataFrame):
        """
        fit 시점에서 user + auto(freq>0.9) special values 합치기
        => self.special_value[col] = [... all special vals ...]
        """
        for col in self.syn_order:
            if col not in X.columns:
                continue
            info = self.col_map[col]
            if info["converted_type"] != "numeric":
                continue

            # 기존에 user가 준 값
            user_vals = self.special_value.get(col, [])

            # auto: freq>0.9
            freq = X[col].value_counts(dropna=False, normalize=True)
            big_ones = freq[freq > 0.9].index.tolist()

            merged = sorted(set(user_vals).union(set(big_ones)))
            if merged:
                self.special_value[col] = merged
            else:
                # 만약 아무도 없으면 굳이 빈 list로 남김
                if col in self.special_value:
                    # user가 줬는데 empty? => 그대로 둘 수도
                    pass

    def _store_date_min(self, X: pd.DataFrame):
        for col in self.syn_order:
            if self.col_type.get(col) == "date":
                arr = pd.to_datetime(X[col], errors="coerce")
                self.date_mins[col] = arr.min()

    def _assign_method_to_cols(self):
        for i, col in enumerate(self.syn_order):
            user_m = self.method.get(col)
            if i == 0:
                chosen = user_m if user_m else "swr"
            else:
                chosen = user_m if user_m else self.default_method
            self.col_map[col]["method"] = chosen

    def _assign_variable_selection(self):
        for i, col in enumerate(self.syn_order):
            if col not in self.variable_selection_:
                self.variable_selection_[col] = self.syn_order[:i]

    # ---------------------- (transform) helpers ----------------------
    def _reorder_columns(self, X: pd.DataFrame) -> pd.DataFrame:
        keep = [c for c in self.syn_order if c in X.columns]
        return X[keep]

    def _convert_date_to_offset(self, X: pd.DataFrame) -> pd.DataFrame:
        for col in self.syn_order:
            if self.col_type.get(col) == "date" and col in X.columns:
                mindt = self.date_mins.get(col)
                X[col] = pd.to_datetime(X[col], errors="coerce")
                if mindt is None:
                    mindt = X[col].min()
                    self.date_mins[col] = mindt
                X[col] = (X[col] - mindt).dt.days
        return X

    def _split_numeric_cols_in_front(self, X: pd.DataFrame) -> pd.DataFrame:
        """
        special_value[col] 에 값이 있으면 => col_cat 생성
        """
        original_order = list(self.syn_order)
        for col in original_order:
            if col not in X.columns:
                continue
            info = self.col_map.get(col, {})
            if info["converted_type"] != "numeric":
                continue
            specials = self.special_value.get(col, [])
            if not specials:
                continue

            cat_col = col + "_cat"

            def cat_mapper(v):
                if pd.isna(v):
                    return -9999
                if v in specials:
                    return v
                return -777

            X[cat_col] = X[col].apply(cat_mapper).astype("category")

            def numeric_mapper(v):
                if pd.isna(v):
                    return np.nan
                if v in specials:
                    return np.nan
                return v

            X[col] = X[col].apply(numeric_mapper)

            if cat_col not in self.syn_order:
                idx = self.syn_order.index(col)
                self.syn_order.insert(idx, cat_col)

            self.col_map[cat_col] = {
                "original_dtype": "category",
                "converted_type": "category",
                "method": self.col_map[col]["method"]
            }
        return X

    def _apply_converted_dtype(self, X: pd.DataFrame) -> pd.DataFrame:
        for col in self.syn_order:
            if col not in X.columns:
                continue
            cinfo = self.col_map[col]
            ctype = cinfo["converted_type"]
            if ctype == "numeric":
                X[col] = pd.to_numeric(X[col], errors="coerce")
            else:
                X[col] = X[col].astype("category")
        return X

    def _convert_offset_to_date(self, X: pd.DataFrame) -> pd.DataFrame:
        for col in self.syn_order:
            if self.col_type.get(col) == "date" and col in X.columns:
                offset = pd.to_numeric(X[col], errors="coerce")
                mindt = self.date_mins.get(col, None)
                if mindt is not None:
                    X[col] = pd.to_timedelta(offset, unit="D") + mindt
        return X

    def _update_varsel_dict_after_split(self, X: pd.DataFrame) -> None:
        new_splits = {}
        for col in self.syn_order:
            if col.endswith("_cat"):
                base = col[:-4]
                if base in self.col_map:
                    new_splits[base] = col

        # 새 col_cat이 아직 varsel에 없으면 기본 predictor
        for col in X.columns:
            if col not in self.variable_selection_ and col in self.syn_order:
                idx = self.syn_order.index(col)
                self.variable_selection_[col] = self.syn_order[:idx]

        # base_col이 predictor인 곳 => cat_col도 추가
        for tgt_col, preds in self.variable_selection_.items():
            updated = set(preds)
            for bcol, ccol in new_splits.items():
                if bcol in updated:
                    updated.add(ccol)
            self.variable_selection_[tgt_col] = list(updated)

    def _merge_splitted_cols(self, X: pd.DataFrame) -> pd.DataFrame:
        splitted_cols = [c for c in self.syn_order if c.endswith("_cat")]
        for cat_col in splitted_cols:
            base_col = cat_col[:-4]  # 예: 'income_cat' -> 'income'
            if base_col not in X.columns or cat_col not in X.columns:
                continue

            # 해당 base_col의 special_value 목록
            specials = self.special_value.get(base_col, [])

            for i in range(len(X)):
                # base_col이 NaN인 경우만 복원 로직
                if pd.isna(X.at[i, base_col]):
                    cat_val = X.at[i, cat_col]

                    # [수정 1] cat_val이 float/int인지, 문자열인지 구분
                    #         만약 문자열이면 float 변환 시도
                    parsed_val = None
                    if isinstance(cat_val, (int, float)):
                        # 이미 숫자
                        parsed_val = float(cat_val)
                    else:
                        # 문자열일 경우
                        try:
                            parsed_val = float(cat_val)
                        except:
                            pass

                    # [수정 2] parsed_val이 specials에 있는지 확인
                    if parsed_val is not None and parsed_val in specials:
                        X.at[i, base_col] = parsed_val
                    else:
                        # cat_val이 -9999 => NaN 유지
                        # cat_val이 -777 => 일반 numeric
                        pass

            # cat_col은 이제 필요 없으므로 제거
            X.drop(columns=[cat_col], inplace=True)

        return X

    # ----------------------
    def get_info(self) -> Dict[str, Any]:
        """
        고정된 키 이름들로 반환
        special_value => {col: [ ... ]}
        """
        orig_dtype_map = {}
        conv_type_map = {}
        method_map = {}
        for c, info in self.col_map.items():
            orig_dtype_map[c] = info.get("original_dtype")
            conv_type_map[c] = info.get("converted_type")
            method_map[c] = info.get("method")

        return {
            "syn_order": self.syn_order,
            "original_dtype": orig_dtype_map,
            "converted_type": conv_type_map,
            "method": method_map,
            # 사용자 + auto합쳐진 special values
            "special_value": self.special_value,  # {col: [ ... ]}
            "date_mins": self.date_mins,
            "variable_selection": self.variable_selection_,
        }
