"""高度な分析。回帰・仮説検定・予測・シミュレーションなど。

analysis.py が「SQLでは書けない集計」を担うのに対し、こちらは
統計モデルを当てる側を担当する。入出力の約束は analysis.py と同じで、
SELECT結果 (columns, rows) を受け取り、表示用の表と所見テキストを返す。

戻り値の共通形:
    {"title": 見出し, "tables": [{"name":..., "columns":[...], "rows":[...]}, ...],
     "notes": [所見の文字列, ...], "meta": {機械可読な値}}

所見(notes)は日本語で書く。数字だけ返してもLLMが読み違えるので、
「有意差あり/なし」「あてはまりが弱い」といった判断まで言語化してここで持たせる。
"""
from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd
from scipy import stats

# _clean / _df / _out / _to_numeric / numeric_columns はこのファイルの末尾（元 analysis.py）にある

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="statsmodels")

# 既定の有意水準。業務レポートで使う 5% を採用する。
ALPHA = 0.05

TEST_METHODS = {
    "ttest_1samp": "1標本t検定（平均が基準値と違うか）",
    "ttest_ind": "2標本t検定（2群の平均差・Welch）",
    "ttest_rel": "対応のあるt検定（同じ対象の前後比較）",
    "mannwhitney": "Mann-WhitneyのU検定（2群・順位）",
    "wilcoxon": "Wilcoxonの符号順位検定（対応あり・順位）",
    "anova": "一元配置分散分析（3群以上の平均差）",
    "kruskal": "Kruskal-Wallis検定（3群以上・順位）",
    "chi2": "カイ二乗検定（独立性・クロス集計）",
    "chi2_goodness": "カイ二乗適合度検定（比率が想定通りか）",
    "proportion": "比率の検定（2群の割合の差）",
    "normality": "正規性の検定（Shapiro-Wilk）",
    "levene": "等分散性の検定（Levene）",
    "correlation": "無相関の検定（相関が偶然か）",
}
REGRESSION_METHODS = {
    "ols": "重回帰（最小二乗法）",
    "logistic": "ロジスティック回帰（0/1の予測）",
    "poisson": "ポアソン回帰（件数の予測）",
}
FORECAST_METHODS = {
    "naive": "直近値をそのまま延長",
    "drift": "直線の傾きで延長",
    "moving_average": "移動平均で延長",
    "linear": "線形トレンド（回帰）",
    "holt": "指数平滑（トレンドあり）",
    "holt_winters": "指数平滑（トレンド＋季節性）",
    "arima": "ARIMA",
}
DISTRIBUTIONS = {
    "norm": "正規分布", "lognorm": "対数正規分布", "expon": "指数分布",
    "gamma": "ガンマ分布", "uniform": "一様分布", "triang": "三角分布",
}
OUTLIER_METHODS_EXT = {
    "iqr": "四分位範囲（箱ひげ図の外側）",
    "zscore": "標準偏差からの距離",
    "modified_zscore": "中央値からの距離（MAD・外れ値に強い）",
    "percentile": "上下の指定パーセンタイル",
    "mahalanobis": "複数列をまとめて見る（マハラノビス距離）",
}


class AnalysisError(Exception):
    """分析できないときの理由（そのまま画面とLLMに見せる）。"""


def _num(df: pd.DataFrame, cols: list, need: int = 1) -> pd.DataFrame:
    """指定列を数値にして、欠損行を落とす。"""
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise AnalysisError(f"列が見つかりません: {', '.join(missing)}"
                            f"（SQLの結果にある列: {', '.join(map(str, df.columns))}）")
    out = _to_numeric(df.copy(), cols)[cols].dropna()
    if len(out) < need:
        raise AnalysisError(f"数値として使える行が {len(out)} 行しかありません"
                            f"（{need}行以上必要）。列の型か抽出条件を見直してください。")
    return out


def _p_note(p: float, what: str) -> str:
    if p is None or (isinstance(p, float) and math.isnan(p)):
        return f"{what}: p値を計算できませんでした。"
    if p < ALPHA:
        return (f"{what}: p値 {p:.4g} < {ALPHA} なので、"
                "偶然とは考えにくい差（有意差あり）です。")
    return (f"{what}: p値 {p:.4g} ≧ {ALPHA} なので、"
            "この結果からは差があるとは言えません（有意差なし）。")


def _effect_note(name: str, value: float) -> str:
    """効果量の目安。p値だけ見て「差がある」と早合点しないための添え物。"""
    a = abs(value)
    if name == "cohen_d":
        size = "小さい" if a < 0.5 else ("中くらい" if a < 0.8 else "大きい")
        return f"効果量 Cohen's d = {value:.3f}（差の大きさは{size}）"
    if name == "eta_squared":
        size = "小さい" if a < 0.06 else ("中くらい" if a < 0.14 else "大きい")
        return f"効果量 η² = {value:.3f}（群による説明力は{size}）"
    if name == "cramers_v":
        size = "弱い" if a < 0.2 else ("中くらい" if a < 0.4 else "強い")
        return f"効果量 Cramer's V = {value:.3f}（関連は{size}）"
    return f"効果量 {name} = {value:.3f}"


def _table(name: str, columns: list, rows: list) -> dict:
    return {"name": name, "columns": [str(c) for c in columns],
            "rows": [[_clean(v) for v in r] for r in rows]}


def _df_table(name: str, df: pd.DataFrame, index_label: str | None = None) -> dict:
    d = df.reset_index() if index_label else df
    if index_label:
        d = d.rename(columns={d.columns[0]: index_label})
    cols, rows = _out(d)
    return _table(name, cols, rows)


# =============================================================================
# 仮説検定
# =============================================================================

def hypothesis_test(columns: list, rows: list, method: str, *,
                    value_col: str | None = None, group_col: str | None = None,
                    value_col2: str | None = None, popmean: float = 0.0,
                    expected: list | None = None, alternative: str = "two-sided",
                    alpha: float = ALPHA) -> dict:
    """統計的仮説検定。method は TEST_METHODS のキー。"""
    if method not in TEST_METHODS:
        raise AnalysisError(f"未対応の検定です: {method}。"
                            f"使えるのは {', '.join(TEST_METHODS)} です。")
    df = _df(columns, rows)
    label = TEST_METHODS[method]
    tables, notes, meta = [], [], {"method": method, "alpha": alpha}

    def groups_of(vcol, gcol):
        d = df[[gcol, vcol]].copy()
        d[vcol] = pd.to_numeric(d[vcol], errors="coerce")
        d = d.dropna()
        gs = [(str(k), g[vcol].to_numpy()) for k, g in d.groupby(gcol)]
        gs = [(k, v) for k, v in gs if len(v) >= 2]
        if len(gs) < 2:
            raise AnalysisError(f"比較できる群が {len(gs)} しかありません"
                                f"（各群2件以上・2群以上が必要）。")
        return gs

    if method == "ttest_1samp":
        x = _num(df, [value_col], 2)[value_col].to_numpy()
        st, p = stats.ttest_1samp(x, popmean, alternative=alternative)
        d = (x.mean() - popmean) / (x.std(ddof=1) or np.nan)
        tables.append(_table("検定結果", ["項目", "値"], [
            ["件数", len(x)], ["平均", x.mean()], ["基準値", popmean],
            ["差", x.mean() - popmean], ["t値", st], ["p値", p]]))
        notes += [_p_note(p, f"{value_col} の平均と基準値 {popmean} の差"),
                  _effect_note("cohen_d", d)]
        meta.update(statistic=float(st), p_value=float(p), effect=float(d))

    elif method in ("ttest_ind", "mannwhitney", "levene"):
        gs = groups_of(value_col, group_col)
        if len(gs) > 2:
            raise AnalysisError(f"この検定は2群までです（いまは {len(gs)}群）。"
                                "3群以上なら anova か kruskal を使ってください。")
        (n1, a), (n2, b) = gs
        if method == "ttest_ind":
            st, p = stats.ttest_ind(a, b, equal_var=False, alternative=alternative)
            sd = math.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1))
                           / max(len(a) + len(b) - 2, 1))
            eff = (a.mean() - b.mean()) / sd if sd else float("nan")
            notes.append(_effect_note("cohen_d", eff))
        elif method == "mannwhitney":
            st, p = stats.mannwhitneyu(a, b, alternative=alternative)
            eff = 2 * st / (len(a) * len(b)) - 1        # rank-biserial
            notes.append(f"効果量 rank-biserial = {eff:.3f}")
        else:
            st, p = stats.levene(a, b)
            eff = float("nan")
        tables.append(_table("群ごとの要約", ["群", "件数", "平均", "中央値", "標準偏差"], [
            [n1, len(a), a.mean(), np.median(a), a.std(ddof=1)],
            [n2, len(b), b.mean(), np.median(b), b.std(ddof=1)]]))
        tables.append(_table("検定結果", ["項目", "値"],
                             [["統計量", st], ["p値", p]]))
        notes.insert(0, _p_note(p, f"{n1} と {n2} の{'ばらつき' if method == 'levene' else value_col}の差"))
        meta.update(statistic=float(st), p_value=float(p), effect=float(eff))

    elif method in ("ttest_rel", "wilcoxon"):
        pair = _num(df, [value_col, value_col2], 2)
        a, b = pair[value_col].to_numpy(), pair[value_col2].to_numpy()
        if method == "ttest_rel":
            st, p = stats.ttest_rel(a, b, alternative=alternative)
            diff = a - b
            eff = diff.mean() / (diff.std(ddof=1) or np.nan)
            notes.append(_effect_note("cohen_d", eff))
        else:
            st, p = stats.wilcoxon(a, b, alternative=alternative)
            eff = float("nan")
        tables.append(_table("対応するデータ", ["項目", value_col, value_col2, "差"], [
            ["件数", len(a), len(b), len(a)],
            ["平均", a.mean(), b.mean(), (a - b).mean()],
            ["中央値", np.median(a), np.median(b), np.median(a - b)]]))
        tables.append(_table("検定結果", ["項目", "値"], [["統計量", st], ["p値", p]]))
        notes.insert(0, _p_note(p, f"{value_col} と {value_col2} の差"))
        meta.update(statistic=float(st), p_value=float(p), effect=float(eff))

    elif method in ("anova", "kruskal"):
        gs = groups_of(value_col, group_col)
        arrays = [v for _, v in gs]
        if method == "anova":
            st, p = stats.f_oneway(*arrays)
            grand = np.concatenate(arrays)
            ss_b = sum(len(v) * (v.mean() - grand.mean()) ** 2 for v in arrays)
            ss_t = ((grand - grand.mean()) ** 2).sum()
            eff = ss_b / ss_t if ss_t else float("nan")
            notes.append(_effect_note("eta_squared", eff))
        else:
            st, p = stats.kruskal(*arrays)
            eff = float("nan")
        tables.append(_table("群ごとの要約", ["群", "件数", "平均", "中央値", "標準偏差"],
                             [[k, len(v), v.mean(), np.median(v), v.std(ddof=1)]
                              for k, v in gs]))
        tables.append(_table("検定結果", ["項目", "値"],
                             [["統計量", st], ["p値", p], ["群の数", len(gs)]]))
        notes.insert(0, _p_note(p, f"{len(gs)}群の {value_col} の差"))
        if p < alpha and len(gs) > 2:
            pairs = []
            for i in range(len(gs)):
                for j in range(i + 1, len(gs)):
                    _, pp = stats.ttest_ind(gs[i][1], gs[j][1], equal_var=False)
                    # Bonferroni: 比較回数ぶん厳しくする
                    n_comp = len(gs) * (len(gs) - 1) / 2
                    pairs.append([gs[i][0], gs[j][0], pp, min(pp * n_comp, 1.0),
                                  "有意" if pp * n_comp < alpha else ""])
            tables.append(_table("どの組み合わせに差があるか（Bonferroni補正）",
                                 ["群1", "群2", "p値", "補正後p値", "判定"], pairs))
            notes.append("どの群どうしに差があるかは「補正後p値」が0.05未満の行を見てください。")
        meta.update(statistic=float(st), p_value=float(p), effect=float(eff))

    elif method == "chi2":
        ct = pd.crosstab(df[group_col], df[value_col])
        st, p, dof, exp = stats.chi2_contingency(ct)
        n = ct.to_numpy().sum()
        v = math.sqrt(st / (n * (min(ct.shape) - 1))) if min(ct.shape) > 1 else float("nan")
        tables.append(_df_table("クロス集計（実測）", ct, index_label=str(group_col)))
        tables.append(_df_table("期待度数（関連が無い場合）",
                                pd.DataFrame(exp, index=ct.index, columns=ct.columns).round(2),
                                index_label=str(group_col)))
        tables.append(_table("検定結果", ["項目", "値"],
                             [["カイ二乗値", st], ["p値", p], ["自由度", dof]]))
        notes += [_p_note(p, f"{group_col} と {value_col} の関連"), _effect_note("cramers_v", v)]
        if (exp < 5).mean() > 0.2:
            notes.append("※ 期待度数が5未満のマスが2割を超えています。"
                         "カテゴリをまとめるか、件数を増やした方が結果は安定します。")
        meta.update(statistic=float(st), p_value=float(p), effect=float(v), dof=int(dof))

    elif method == "chi2_goodness":
        counts = df[value_col].value_counts().sort_index()
        exp = np.array(expected, dtype=float) if expected else np.full(len(counts),
                                                                      counts.sum() / len(counts))
        exp = exp * counts.sum() / exp.sum()
        st, p = stats.chisquare(counts.to_numpy(), exp)
        tables.append(_table("実測と期待", ["区分", "実測", "期待"],
                             [[str(k), int(v), round(e, 2)]
                              for k, v, e in zip(counts.index, counts, exp)]))
        tables.append(_table("検定結果", ["項目", "値"], [["カイ二乗値", st], ["p値", p]]))
        notes.append(_p_note(p, f"{value_col} の分布と想定の違い"))
        meta.update(statistic=float(st), p_value=float(p))

    elif method == "proportion":
        ct = pd.crosstab(df[group_col], df[value_col])
        if ct.shape != (2, 2):
            raise AnalysisError(f"比率の検定は2群×2値のときに使えます"
                                f"（いまは {ct.shape[0]}群×{ct.shape[1]}値）。")
        st, p, dof, _ = stats.chi2_contingency(ct, correction=True)
        rates = ct.iloc[:, 1] / ct.sum(axis=1)
        tables.append(_table("群ごとの比率", ["群", "件数", f"{ct.columns[1]}の数", "比率"],
                             [[str(i), int(ct.loc[i].sum()), int(ct.loc[i].iloc[1]),
                               round(float(rates[i]), 4)] for i in ct.index]))
        tables.append(_table("検定結果", ["項目", "値"], [["カイ二乗値", st], ["p値", p]]))
        notes += [_p_note(p, "2群の比率の差"),
                  f"比率の差 = {abs(rates.iloc[0] - rates.iloc[1]):.4f}"]
        meta.update(statistic=float(st), p_value=float(p))

    elif method == "normality":
        x = _num(df, [value_col], 3)[value_col].to_numpy()
        if len(x) > 5000:
            x = np.random.default_rng(0).choice(x, 5000, replace=False)
            notes.append("※ 件数が多いので5000件を無作為抽出して検定しました。")
        st, p = stats.shapiro(x)
        tables.append(_table("検定結果", ["項目", "値"], [
            ["件数", len(x)], ["歪度", stats.skew(x)], ["尖度", stats.kurtosis(x)],
            ["W統計量", st], ["p値", p]]))
        notes.append(f"正規性: p値 {p:.4g} "
                     + ("< 0.05 なので正規分布とは言いにくいです"
                        "（順位を使う検定 mannwhitney / kruskal が無難）。"
                        if p < alpha else "≧ 0.05 なので正規分布として扱って差し支えありません。"))
        meta.update(statistic=float(st), p_value=float(p))

    elif method == "correlation":
        pair = _num(df, [value_col, value_col2], 3)
        r, p = stats.pearsonr(pair[value_col], pair[value_col2])
        rho, prho = stats.spearmanr(pair[value_col], pair[value_col2])
        tables.append(_table("検定結果", ["項目", "値"], [
            ["件数", len(pair)], ["ピアソンr", r], ["p値", p],
            ["スピアマンρ", rho], ["p値(ρ)", prho]]))
        notes += [_p_note(p, f"{value_col} と {value_col2} の相関"),
                  f"相関係数 r = {r:.3f}（"
                  + ("ほぼ無相関" if abs(r) < 0.2 else
                     "弱い相関" if abs(r) < 0.4 else
                     "中程度の相関" if abs(r) < 0.7 else "強い相関") + "）",
                  "相関は因果ではありません。第三の要因が両方を動かしている可能性を検討してください。"]
        meta.update(statistic=float(r), p_value=float(p))

    return {"title": f"{label}", "tables": tables, "notes": notes, "meta": meta}


# =============================================================================
# 回帰
# =============================================================================

def _holdout_score(sm, method: str, X: pd.DataFrame, y: pd.Series, cols) -> dict | None:
    """行の3割を隠して学習し、隠した側での成績を返す。

    学習に使ったデータで測ったR²は必ず良く出る。「予測に使えるのか」を
    聞かれたときに、その数字を答えると嘘になるので、別データで測り直す。
    """
    rng = np.random.default_rng(0)
    idx = rng.permutation(len(X))
    cut = int(len(X) * 0.7)
    tr, te = idx[:cut], idx[cut:]
    if len(te) < 5 or cut <= X.shape[1] + 1:
        return None
    Xtr = sm.add_constant(X.iloc[tr], has_constant="add")
    Xte = sm.add_constant(X.iloc[te], has_constant="add").reindex(columns=cols, fill_value=0.0)
    ytr, yte = y.iloc[tr], y.iloc[te]
    try:
        if method == "ols":
            m = sm.OLS(ytr, Xtr).fit()
            pred = np.asarray(m.predict(Xte), dtype=float)
            act = yte.to_numpy(dtype=float)
            ss_res = float(np.sum((act - pred) ** 2))
            ss_tot = float(np.sum((act - act.mean()) ** 2))
            r2 = 1 - ss_res / ss_tot if ss_tot else float("nan")
            mae = float(np.mean(np.abs(act - pred)))
            rows = [["検証データのR²", round(r2, 3)], ["平均絶対誤差(MAE)", round(mae, 4)],
                    ["学習に使った件数", len(tr)], ["検証に使った件数", len(te)]]
            note = (f"学習に使っていないデータでのR² = {r2:.3f}"
                    + ("。学習時とほぼ同じで、予測にも使えます。" if r2 > 0.5 else
                       "。学習時より大きく落ちる場合、この式は手元のデータに"
                       "合わせすぎていて、将来の予測には向きません。"))
        elif method == "logistic":
            m = sm.Logit(ytr, Xtr).fit(disp=0)
            p = np.asarray(m.predict(Xte), dtype=float)
            act = yte.to_numpy(dtype=float)
            acc = float(np.mean((p >= 0.5).astype(float) == act))
            base = float(max(act.mean(), 1 - act.mean()))
            rows = [["正解率", round(acc, 3)], ["全部多い方に賭けた場合", round(base, 3)],
                    ["検証に使った件数", len(te)]]
            note = (f"学習に使っていないデータでの正解率 = {acc:.1%}"
                    f"（何も考えず多い方に賭けると {base:.1%}）。"
                    + ("上回っているので、判別に意味があります。" if acc > base + 0.02 else
                       "差が無いため、この説明変数では判別できていません。"))
        else:
            return None
    except Exception:
        return None
    return {"rows": rows, "note": note}


def regression(columns: list, rows: list, target: str, features: list,
               method: str = "ols", predict: list | None = None,
               alpha: float = ALPHA) -> dict:
    """回帰分析。係数・有意性・あてはまり・診断をまとめて返す。"""
    import statsmodels.api as sm

    if method not in REGRESSION_METHODS:
        raise AnalysisError(f"未対応の手法です: {method}。"
                            f"使えるのは {', '.join(REGRESSION_METHODS)} です。")
    df = _df(columns, rows)
    features = [f for f in (features or []) if f != target]
    if not features:
        raise AnalysisError("説明変数を1つ以上指定してください。")

    # 文字列の列はダミー変数にする（部署などをそのまま説明変数にできるように）
    use = df[[target] + features].copy()
    use[target] = pd.to_numeric(use[target], errors="coerce")
    num_feats, cat_feats = [], []
    for f in features:
        s = pd.to_numeric(use[f], errors="coerce")
        if s.notna().mean() >= 0.8:
            use[f] = s
            num_feats.append(f)
        else:
            cat_feats.append(f)
    use = use.dropna(subset=[target] + num_feats)
    X = use[num_feats].copy()
    for f in cat_feats:
        d = pd.get_dummies(use[f].astype(str), prefix=f, drop_first=True, dtype=float)
        X = pd.concat([X, d], axis=1)
    X = X.dropna()
    y = use.loc[X.index, target]
    if len(X) <= X.shape[1] + 1:
        raise AnalysisError(f"データが {len(X)} 行しかなく、説明変数 {X.shape[1]} 個に対して"
                            "足りません。行を増やすか説明変数を減らしてください。")

    Xc = sm.add_constant(X, has_constant="add")
    tables, notes = [], []
    if method == "ols":
        model = sm.OLS(y, Xc).fit()
        fit_rows = [["決定係数 R²", model.rsquared], ["自由度調整済みR²", model.rsquared_adj],
                    ["F値", model.fvalue], ["モデルのp値", model.f_pvalue],
                    ["AIC", model.aic], ["件数", int(model.nobs)]]
    elif method == "logistic":
        uniq = set(pd.unique(y.dropna()))
        if not uniq <= {0, 1}:
            raise AnalysisError(f"ロジスティック回帰の目的変数は0か1にしてください"
                                f"（いまの値: {sorted(uniq)[:5]}）。"
                                "SQL側で CASE WHEN ... THEN 1 ELSE 0 END にしてください。")
        model = sm.Logit(y, Xc).fit(disp=0)
        fit_rows = [["疑似R²(McFadden)", model.prsquared], ["対数尤度", model.llf],
                    ["AIC", model.aic], ["件数", int(model.nobs)]]
    else:
        model = sm.GLM(y, Xc, family=sm.families.Poisson()).fit()
        fit_rows = [["対数尤度", model.llf], ["AIC", model.aic], ["件数", int(model.nobs)]]

    coef = pd.DataFrame({
        "変数": model.params.index,
        "係数": model.params.to_numpy(),
        "標準誤差": model.bse.to_numpy(),
        "p値": model.pvalues.to_numpy(),
    })
    ci = model.conf_int()
    coef["95%下限"], coef["95%上限"] = ci.iloc[:, 0].to_numpy(), ci.iloc[:, 1].to_numpy()
    coef["判定"] = np.where(coef["p値"] < alpha, "有意", "")
    if method == "logistic":
        coef["オッズ比"] = np.exp(coef["係数"])
    elif method == "poisson":
        coef["倍率"] = np.exp(coef["係数"])
    cols, rws = _out(coef)
    tables.append(_table("係数", cols, rws))
    tables.append(_table("あてはまり", ["項目", "値"], fit_rows))

    sig = coef[(coef["p値"] < alpha) & (coef["変数"] != "const")]
    if len(sig):
        top = sig.reindex(sig["係数"].abs().sort_values(ascending=False).index)
        notes.append("効いている変数: " + "、".join(
            f"{r['変数']}（係数 {r['係数']:.4g}）" for _, r in top.head(5).iterrows()))
    else:
        notes.append("p値が0.05を下回る説明変数はありませんでした。"
                     "この説明変数の組み合わせでは目的変数を説明できていません。")
    if method == "ols":
        notes.append(f"あてはまり: R² = {model.rsquared:.3f}（目的変数のばらつきの"
                     f"{model.rsquared * 100:.1f}%を説明）。"
                     + ("説明力は弱いので、変数の追加を検討してください。"
                        if model.rsquared < 0.3 else ""))
        # 多重共線性。説明変数どうしが強く相関していると係数の解釈ができない
        if X.shape[1] > 1:
            from statsmodels.stats.outliers_influence import variance_inflation_factor
            vifs = []
            arr = Xc.to_numpy(dtype=float)
            for i, name in enumerate(Xc.columns):
                if name == "const":
                    continue
                try:
                    vifs.append([name, variance_inflation_factor(arr, i)])
                except Exception:
                    pass
            if vifs:
                tables.append(_table("多重共線性(VIF)", ["変数", "VIF"], vifs))
                bad = [n for n, v in vifs if v and v > 10]
                if bad:
                    notes.append(f"※ VIFが10を超える変数（{', '.join(bad)}）があります。"
                                 "説明変数どうしが似すぎていて、係数の意味を読み違えます。"
                                 "どちらかを外してください。")
        resid = model.resid
        dw = float(sm.stats.durbin_watson(resid))
        notes.append(f"残差の自己相関 Durbin-Watson = {dw:.2f}"
                     + ("（2から離れており、時系列の並びが残差に残っています）"
                        if dw < 1.5 or dw > 2.5 else "（おおむね問題なし）"))

    # 未知のデータでも通用するか。学習に使っていない行で確かめる。
    # R²は当てはめた本人のデータで測ると必ず良く出るので、予測用途では過大評価になる。
    if len(X) >= 30:
        hold = _holdout_score(sm, method, X, y, Xc.columns)
        if hold:
            tables.append(_table("検証（学習に使っていないデータでの成績）",
                                 ["項目", "値"], hold["rows"]))
            notes.append(hold["note"])
    notes.append("回帰は相関の構造を示すもので、因果を証明するものではありません。")

    meta = {"method": method, "n": len(X),
            "coefficients": {str(k): _clean(v) for k, v in model.params.items()},
            "r2": _clean(getattr(model, "rsquared", None)),
            "formula": f"{target} ~ " + " + ".join(map(str, X.columns))}

    if predict:
        rowsp = []
        for case in predict:
            vec = {"const": 1.0}
            for c in X.columns:
                vec[c] = float(case.get(c, 0) or 0)
            xs = pd.DataFrame([[vec.get(c, 0.0) for c in Xc.columns]], columns=Xc.columns)
            yhat = float(model.predict(xs)[0])
            rowsp.append([", ".join(f"{k}={v}" for k, v in case.items()), yhat])
        tables.append(_table("予測", ["入力", "予測値"], rowsp))

    return {"title": REGRESSION_METHODS[method], "tables": tables,
            "notes": notes, "meta": meta}


# =============================================================================
# 予測（時系列）
# =============================================================================

def lag_correlation(columns: list, rows: list, target: str, features: list | None,
                    max_lag: int = 6, method: str = "pearson") -> dict:
    """時差相関。「今月の広告費は翌月の売上に効くのか」を見る。

    行は時点の昇順に並んでいる前提（SQLで ORDER BY してもらう）。
    lag=k は「説明側をk期ずらして、後の target と突き合わせる」の意味。
    """
    df = _df(columns, rows)
    if target not in df.columns:
        raise AnalysisError(f"target の列 '{target}' がありません。")
    feats = [c for c in (features or df.columns) if c != target and c in df.columns]
    feats = [c for c in feats if pd.to_numeric(df[c], errors="coerce").notna().mean() >= 0.8]
    if not feats:
        raise AnalysisError("比べる数値列がありません。columns で指定してください。")
    y = pd.to_numeric(df[target], errors="coerce")
    max_lag = max(1, min(int(max_lag or 6), max(1, len(df) // 3)))

    out, best_lines = [], []
    for f in feats:
        x = pd.to_numeric(df[f], errors="coerce")
        row, best = [f], (0.0, 0)
        for k in range(0, max_lag + 1):
            pair = pd.DataFrame({"x": x.shift(k), "y": y}).dropna()
            r = (float(pair["x"].corr(pair["y"], method=method))
                 if len(pair) >= 3 else float("nan"))
            row.append(None if math.isnan(r) else round(r, 3))
            if not math.isnan(r) and abs(r) > abs(best[0]):
                best = (r, k)
        out.append(row)
        if best[1] > 0:
            best_lines.append(
                f"{f} は {best[1]}期ずらしたときが最も強く（相関 {best[0]:+.2f}）、"
                f"同時点（{row[1] if row[1] is not None else '—'}）より強い。"
                f"{f}の効果が{best[1]}期あとに出ている可能性があります。")
        else:
            best_lines.append(f"{f} は同時点が最も強い（相関 {best[0]:+.2f}）。ずれは見られません。")

    notes = ["lag=k は「説明側をk期ずらして、あとの " + target + " と比べた」という意味です。"]
    notes += best_lines
    notes.append("時差があるからといって原因とは限りません。両方が同じ季節要因で"
                 "動いているだけのこともあります。")
    return {"title": f"{target} との時差相関（最大{max_lag}期）",
            "tables": [_table("時差ごとの相関",
                              ["列"] + [f"lag={k}" for k in range(0, max_lag + 1)], out)],
            "notes": notes, "meta": {"max_lag": max_lag}}


def partial_correlation(columns: list, rows: list, features: list | None,
                        control: list, method: str = "pearson") -> dict:
    """偏相関。指定した列の影響を取り除いてから相関を見る。

    「気温を除いても売上と来店数は連動しているか」のように、
    第3の変数のせいで相関して見えているだけ、を切り分けるためのもの。
    """
    df = _df(columns, rows)
    ctrl = [c for c in (control or []) if c in df.columns]
    if not ctrl:
        raise AnalysisError("control（影響を取り除きたい列）を1つ以上指定してください。")
    feats = [c for c in (features or df.columns) if c in df.columns and c not in ctrl]
    feats = [c for c in feats if pd.to_numeric(df[c], errors="coerce").notna().mean() >= 0.8]
    if len(feats) < 2:
        raise AnalysisError("偏相関には数値列が2つ以上必要です。")

    d = _num(df, feats + ctrl, 3)
    # 各列から control 成分を回帰で抜き、その残差どうしの相関を見る
    resid = {}
    c_mat = np.column_stack([np.ones(len(d))] + [d[c].to_numpy(float) for c in ctrl])
    for f in feats:
        yv = d[f].to_numpy(float)
        beta, *_ = np.linalg.lstsq(c_mat, yv, rcond=None)
        resid[f] = yv - c_mat @ beta
    rd = pd.DataFrame(resid)
    pc = rd.corr(method=method).round(3)
    raw = d[feats].corr(method=method).round(3)

    table = pc.reset_index().rename(columns={"index": "列"})
    cols, rws = _out(table)

    notes = [f"{'、'.join(ctrl)} の影響を取り除いた相関です。"]
    for i, a in enumerate(feats):
        for b in feats[i + 1:]:
            r0, r1 = float(raw.loc[a, b]), float(pc.loc[a, b])
            if abs(r0) >= 0.3 and abs(r1) < abs(r0) * 0.5:
                notes.append(f"{a} と {b}: 見かけ {r0:+.2f} → 取り除くと {r1:+.2f}。"
                             f"この関係の多くは {'、'.join(ctrl)} で説明できます。")
            elif abs(r1) >= 0.3:
                notes.append(f"{a} と {b}: 見かけ {r0:+.2f} → 取り除いても {r1:+.2f}。"
                             "取り除いた変数だけでは説明できない関係が残っています。")
    if len(notes) == 1:
        notes.append("目立った変化はありませんでした。")
    return {"title": f"偏相関（{'、'.join(ctrl)} の影響を除く）",
            "tables": [_table("偏相関行列", cols, rws)], "notes": notes,
            "meta": {"control": ctrl}}


def detect_anomalies(columns: list, rows: list, time_col: str, value_col: str,
                     window: int = 7, threshold: float = 3.0,
                     season_length: int | None = None,
                     changepoints: bool = True) -> dict:
    """時系列の「いつもと違う時点」と「いつから変わったか」を出す。

    静的な外れ値（outliers）は、右肩上がりのデータだと最近の値を全部
    外れ値と言ってしまう。ここでは直前の期間と比べるので、水準が変わっても
    「その時点として不自然か」を見られる。
    """
    df = _df(columns, rows)
    for c in (time_col, value_col):
        if c not in df.columns:
            raise AnalysisError(f"列が見つかりません: {c}")
    d = df[[time_col, value_col]].copy()
    d[value_col] = pd.to_numeric(d[value_col], errors="coerce")
    d = d.dropna().sort_values(time_col).reset_index(drop=True)
    if len(d) < 8:
        raise AnalysisError(f"異常検知には最低8点必要です（いま {len(d)} 点）。")

    y = d[value_col]
    window = max(3, min(int(window or 7), max(3, len(d) // 3)))
    base = y

    notes = []
    if season_length and len(d) >= season_length * 2:
        # 曜日・月の周期そのものは異常ではない。季節ぶんを引いてから見る
        from statsmodels.tsa.seasonal import seasonal_decompose
        try:
            dec = seasonal_decompose(y, period=int(season_length),
                                     model="additive", extrapolate_trend="freq")
            base = y - dec.seasonal
            notes.append(f"周期{season_length}の季節変動を取り除いてから判定しました。")
        except Exception:
            pass

    # 中央値と MAD（中央絶対偏差）。平均と標準偏差だと異常値自身に引っ張られる
    med = base.rolling(window, center=True, min_periods=3).median()
    mad = (base - med).abs().rolling(window, center=True, min_periods=3).median()
    scale = mad * 1.4826                       # MADを標準偏差の尺度に合わせる係数
    scale = scale.replace(0, np.nan).fillna(base.std(ddof=0) or 1.0)
    score = (base - med) / scale

    d["移動中央値"] = med.round(4)
    d["乖離スコア"] = score.round(2)
    d["判定"] = np.where(score.abs() >= threshold,
                         np.where(score > 0, "高い", "低い"), "")
    hit = d[d["判定"] != ""].copy()
    hit = hit.reindex(hit["乖離スコア"].abs().sort_values(ascending=False).index)

    cols, rws = _out(hit[[time_col, value_col, "移動中央値", "乖離スコア", "判定"]].head(100))
    tables = [_table("いつもと違う時点", cols, rws)]

    notes.insert(0, f"前後{window}期の中央値から、ばらつきの{threshold}倍以上離れた時点を"
                    f"拾いました。{len(hit)}件（全{len(d)}期中 {len(hit) / len(d) * 100:.1f}%）。")
    if len(hit):
        worst = hit.iloc[0]
        notes.append(f"最も外れているのは {worst[time_col]}（{value_col} = "
                     f"{worst[value_col]:,.4g}、通常は {worst['移動中央値']:,.4g} 前後）。")
    else:
        notes.append("目立って外れた時点はありませんでした。")

    cps = []
    if changepoints and len(d) >= 12:
        cps = _changepoints(base.to_numpy(dtype=float))
        if cps:
            crows = []
            for i in cps:
                before, after = base[:i].mean(), base[i:].mean()
                crows.append([str(d[time_col].iloc[i]), round(float(before), 4),
                              round(float(after), 4),
                              f"{(after - before) / before * 100:+.1f}%" if before else "—"])
            tables.append(_table("水準が変わった時点", ["時点", "その前の平均", "その後の平均", "変化"],
                                 crows))
            notes.append("「水準が変わった時点」は、そこを境に平均が段差になっている所です。"
                         "施策や仕様変更の日付と突き合わせてみてください。")
        else:
            notes.append("平均が段差になるような変化点は見つかりませんでした。")

    return {"title": f"{value_col} の異常検知",
            "tables": tables, "notes": notes,
            "meta": {"anomalies": len(hit), "window": window,
                     "threshold": threshold, "changepoints": len(cps)}}


def _changepoints(y: np.ndarray, max_cuts: int = 3, min_seg: int = 4) -> list:
    """平均が切り替わった位置を探す（二分割を繰り返す素朴な方法）。

    残差平方和がいちばん減る切れ目を選び、減り方が小さくなったらやめる。
    ライブラリを増やさずに「いつから変わったか」に答えるための最小限の実装。
    """
    def best_cut(a: int, b: int):
        seg = y[a:b]
        if len(seg) < min_seg * 2:
            return None
        total = float(((seg - seg.mean()) ** 2).sum())
        best, gain = None, 0.0
        for i in range(min_seg, len(seg) - min_seg):
            left, right = seg[:i], seg[i:]
            after = float(((left - left.mean()) ** 2).sum()
                          + ((right - right.mean()) ** 2).sum())
            if total - after > gain:
                best, gain = a + i, total - after
        # 全体のばらつきの1割も説明できない切れ目は、ただの揺らぎとみなす
        return best if best is not None and gain > total * 0.10 else None

    cuts, segments = [], [(0, len(y))]
    while len(cuts) < max_cuts and segments:
        found = []
        for a, b in segments:
            c = best_cut(a, b)
            if c is not None:
                found.append((c, a, b))
        if not found:
            break
        c, a, b = found[0]
        cuts.append(c)
        segments = [s for s in segments if s != (a, b)] + [(a, c), (c, b)]
    return sorted(cuts)


def survival_analysis(columns: list, rows: list, duration_col: str,
                      event_col: str | None = None, group_col: str | None = None,
                      fit_weibull: bool = True) -> dict:
    """生存時間分析。「どれだけ持つか」「いつ辞めるか」を扱う。

    設備の故障間隔（MTBF）、社員の在籍期間、顧客の継続期間に同じ道具が使える。
    event_col は「そのできごとが起きたか（1）／まだ起きていないか（0）」。
    まだ起きていない分（打ち切り）を捨てて平均を取ると、必ず短く見積もる。
    """
    df = _df(columns, rows)
    if duration_col not in df.columns:
        raise AnalysisError(f"列が見つかりません: {duration_col}")
    d = df.copy()
    d[duration_col] = pd.to_numeric(d[duration_col], errors="coerce")
    d = d[d[duration_col].notna() & (d[duration_col] >= 0)]
    if len(d) < 5:
        raise AnalysisError(f"分析には最低5件必要です（いま {len(d)} 件）。")
    if event_col and event_col in d.columns:
        ev = pd.to_numeric(d[event_col], errors="coerce").fillna(0)
        d["_event"] = (ev > 0).astype(int)
    else:
        d["_event"] = 1                     # 指定が無ければ全件で起きたものとして扱う

    def km(sub: pd.DataFrame):
        """カプラン・マイヤー法。打ち切りを含めても偏らない継続率の出し方。"""
        t = np.sort(sub.loc[sub["_event"] == 1, duration_col].unique())
        surv, out = 1.0, []
        for ti in t:
            at_risk = int((sub[duration_col] >= ti).sum())
            died = int(((sub[duration_col] == ti) & (sub["_event"] == 1)).sum())
            if at_risk:
                surv *= (1 - died / at_risk)
            out.append([float(ti), at_risk, died, round(surv, 4)])
        return out

    def median_surv(curve):
        for ti, _, _, s in curve:
            if s <= 0.5:
                return ti
        return None

    tables, notes = [], []
    groups = [(None, d)] if not (group_col and group_col in d.columns) \
        else list(d.groupby(group_col))
    summary = []
    for name, sub in groups:
        curve = km(sub)
        label = "全体" if name is None else str(name)
        if name is None:
            cols = ["時間", "対象数", "発生数", "継続率"]
            tables.append(_table("継続率（カプラン・マイヤー）", cols, curve))
        med = median_surv(curve)
        events = int(sub["_event"].sum())
        mtbf = float(sub.loc[sub["_event"] == 1, duration_col].mean()) if events else None
        summary.append([label, len(sub), events,
                        round(mtbf, 3) if mtbf is not None else None,
                        med if med is not None else "未到達"])
    tables.insert(0, _table("要約", [group_col or "対象", "件数", "発生件数",
                                    "平均(発生ぶんのみ)", "継続率が50%になる時間"], summary))

    censored = int((d["_event"] == 0).sum())
    if censored:
        notes.append(f"まだ起きていない（打ち切り）が {censored} 件あります。"
                     "これを捨てて平均すると短く見積もるため、継続率は"
                     "カプラン・マイヤー法で計算しています。")
    for row in summary:
        notes.append(f"{row[0]}: {row[1]}件中 {row[2]}件で発生。"
                     f"継続率が50%を切るのは {row[4]}。")

    if fit_weibull and int(d["_event"].sum()) >= 8:
        # 形状パラメータは「時間とともに壊れやすくなるか」を表す
        try:
            obs = d.loc[d["_event"] == 1, duration_col]
            obs = obs[obs > 0]
            shape, loc, scale = stats.weibull_min.fit(obs, floc=0)
            trend = ("時間とともに起きやすくなる（摩耗・劣化型）" if shape > 1.2 else
                     "時間とともに起きにくくなる（初期不良型）" if shape < 0.8 else
                     "時間によらず一定の確率で起きる（偶発型）")
            tables.append(_table("Weibull分布の当てはめ",
                                 ["項目", "値"],
                                 [["形状パラメータ m", round(float(shape), 3)],
                                  ["尺度パラメータ η", round(float(scale), 3)],
                                  ["読み方", trend]]))
            notes.append(f"Weibullの形状パラメータ = {shape:.2f} → {trend}。"
                         + ("予防交換の周期を決める根拠になります。" if shape > 1.2 else
                            "初期の選別・慣らしが効きます。" if shape < 0.8 else
                            "定期交換をしても発生率は下がりません。"))
        except Exception:
            pass

    return {"title": "生存時間分析", "tables": tables, "notes": notes,
            "meta": {"n": len(d), "events": int(d["_event"].sum()),
                     "censored": censored}}


def _best_arima_order(y: np.ndarray) -> tuple:
    """ARIMAの次数を情報量規準(AIC)で選ぶ。

    以前は (1,1,1) 固定だった。データによっては当てはまらないのに
    「ARIMAで予測した」とだけ言うことになるので、素直な範囲を総当たりする。
    """
    import warnings

    from statsmodels.tsa.api import ARIMA

    best, best_aic = (1, 1, 1), np.inf
    s = pd.Series(y)
    # 総当たりの途中で収束しない組み合わせは当然出る。警告はログを埋めるだけなので黙らせる
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for p in range(0, 3):
            for dd in range(0, 2):
                for q in range(0, 3):
                    if p == 0 and q == 0:
                        continue
                    try:
                        aic = float(ARIMA(s, order=(p, dd, q)).fit().aic)
                    except Exception:
                        continue
                    if np.isfinite(aic) and aic < best_aic:
                        best, best_aic = (p, dd, q), aic
    return best


def _backtest_note(columns: list, rows: list, time_col: str, value_col: str,
                   method: str, season_length: int | None, window: int,
                   exog: dict | None) -> tuple:
    """原点をずらしながら何度も予測して、誤差率(MAPE)の平均を出す。

    末尾を1回だけ隠す方式だと、たまたま当たった／外れただけの数字になる。
    予測できる範囲で3回まで試し、ばらつきも見えるようにする。
    """
    n = len(rows)
    hold = max(1, min(3, n // 6))
    folds, errs, maes, skipped = min(3, max(1, (n - 8) // hold)) if n >= 10 else 1, [], [], 0
    scale = float(np.nanmean(np.abs(pd.to_numeric(
        [r[list(columns).index(value_col)] for r in rows], errors="coerce"))))
    for f in range(folds):
        cut = n - hold * (f + 1)
        if cut < 6:
            break
        try:
            back = forecast(columns, rows[:cut], time_col, value_col,
                            periods=hold, method=method,
                            season_length=season_length, window=window,
                            exog=exog, _backtest=False)
            got = np.array([r[1] for r in back["tables"][1]["rows"]], dtype=float)
        except Exception:
            continue
        act = np.array([pd.to_numeric(r[list(columns).index(value_col)], errors="coerce")
                        for r in rows[cut:cut + hold]], dtype=float)
        if len(act) != len(got) or np.isnan(act).any():
            continue
        maes.append(float(np.mean(np.abs(act - got))))
        # 実績が0に近い期を割り算に含めると誤差率が跳ね上がる。数だけ覚えて除く
        small = np.abs(act) < max(scale * 0.01, 1e-9)
        skipped += int(small.sum())
        with np.errstate(divide="ignore", invalid="ignore"):
            e = np.abs((act - got) / np.where(small, np.nan, act))
        e = e[~np.isnan(e)]
        if len(e):
            errs.append(float(np.mean(e)) * 100)
    return {"mape": float(np.mean(errs)) if errs else None,
            "sd": float(np.std(errs)) if len(errs) > 1 else None,
            "mae": float(np.mean(maes)) if maes else None,
            "scale": scale, "folds": len(maes), "hold": hold, "skipped": skipped}


def forecast(columns: list, rows: list, time_col: str, value_col: str,
             periods: int = 6, method: str = "auto",
             season_length: int | None = None, window: int = 3,
             exog: dict | None = None, _backtest: bool = True) -> dict:
    """将来の値を予測する。時系列は time_col の昇順に並べ替えて使う。

    exog を渡すと説明変数つきで予測する（例: 広告費を来期こう置いたら売上はどうなるか）。
      {"columns": ["広告費"], "future": [[120], [130], ...]}
    """
    df = _df(columns, rows)
    if time_col not in df.columns or value_col not in df.columns:
        raise AnalysisError(f"列が見つかりません（{time_col} / {value_col}）。")
    keep = [time_col, value_col] + list((exog or {}).get("columns") or [])
    missing = [c for c in keep if c not in df.columns]
    if missing:
        raise AnalysisError(f"列が見つかりません: {', '.join(missing)}")
    d = df[keep].copy()
    for c in keep[1:]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna().sort_values(time_col)
    if len(d) < 4:
        raise AnalysisError(f"予測には最低4点必要です（いま {len(d)} 点）。")
    y = d[value_col].to_numpy(dtype=float)
    labels = [str(v) for v in d[time_col]]
    periods = max(1, min(int(periods or 6), 120))

    ex_cols = list((exog or {}).get("columns") or [])
    ex_future = (exog or {}).get("future") or []
    if ex_cols:
        # 説明変数つきは ARIMA(X) でしか扱えないので、方法を寄せる
        if len(ex_future) != periods:
            raise AnalysisError(
                f"説明変数つきの予測には、予測する期数と同じ数だけ将来の値が要ります"
                f"（periods={periods} に対して future は {len(ex_future)} 件）。")
        method = "arima"

    if method == "auto":
        if season_length and len(y) >= season_length * 2:
            method = "holt_winters"
        elif len(y) >= 8:
            method = "holt"
        else:
            method = "linear"
    if method not in FORECAST_METHODS:
        raise AnalysisError(f"未対応の予測方法です: {method}。"
                            f"使えるのは {', '.join(FORECAST_METHODS)} です。")

    notes, lower, upper = [], None, None
    idx = np.arange(len(y), dtype=float)
    future_idx = np.arange(len(y), len(y) + periods, dtype=float)

    if method == "naive":
        pred = np.full(periods, y[-1])
    elif method == "drift":
        slope = (y[-1] - y[0]) / max(len(y) - 1, 1)
        pred = y[-1] + slope * np.arange(1, periods + 1)
    elif method == "moving_average":
        w = max(2, min(int(window or 3), len(y)))
        pred = np.full(periods, y[-w:].mean())
    elif method == "linear":
        sl, ic, r, p, se = stats.linregress(idx, y)
        pred = ic + sl * future_idx
        resid_sd = np.std(y - (ic + sl * idx), ddof=2) if len(y) > 2 else 0.0
        lower, upper = pred - 1.96 * resid_sd, pred + 1.96 * resid_sd
        notes.append(f"傾き = {sl:.4g}/期（{'増加' if sl > 0 else '減少'}傾向）、"
                     f"決定係数 R² = {r ** 2:.3f}、傾きのp値 = {p:.4g}")
    else:
        from statsmodels.tsa.api import ARIMA, ExponentialSmoothing
        s = pd.Series(y)
        try:
            if method == "holt":
                fit = ExponentialSmoothing(s, trend="add").fit()
            elif method == "holt_winters":
                if not season_length or len(y) < season_length * 2:
                    raise AnalysisError(
                        f"季節性ありの予測には、季節の長さ（season_length）の2周期ぶん"
                        f"以上のデータが必要です（いま {len(y)}点 / 季節 {season_length}）。")
                fit = ExponentialSmoothing(s, trend="add", seasonal="add",
                                           seasonal_periods=int(season_length)).fit()
            else:
                # 列名を持たせたまま渡す。そうしないと係数が x1 になり、どの変数か分からなくなる
                ex_hist = (d[ex_cols].astype(float).reset_index(drop=True)
                           if ex_cols else None)
                order = _best_arima_order(y)
                fit = ARIMA(s, order=order, exog=ex_hist).fit()
                notes.append(f"ARIMAの次数は当てはまりの良さ(AIC)で選びました: {order}")
                if ex_cols:
                    coefs = ", ".join(
                        f"{c}={float(fit.params.get(c, float('nan'))):+.4g}" for c in ex_cols)
                    notes.append(f"説明変数の係数（1単位増えたときの{value_col}への効き）: {coefs}")
            if ex_cols:
                fut = pd.DataFrame(list(ex_future), columns=ex_cols).astype(float)
                pred = np.asarray(fit.forecast(periods, exog=fut), dtype=float)
            else:
                pred = np.asarray(fit.forecast(periods), dtype=float)
            resid_sd = float(np.std(fit.resid, ddof=1)) if len(fit.resid) > 1 else 0.0
            lower, upper = pred - 1.96 * resid_sd, pred + 1.96 * resid_sd
        except AnalysisError:
            raise
        except Exception as e:
            raise AnalysisError(f"{FORECAST_METHODS[method]}の当てはめに失敗しました: {e}。"
                                "linear など簡単な方法を試してください。") from e

    fut_labels = [f"+{i}期" for i in range(1, periods + 1)]
    frows = []
    for i, v in enumerate(pred):
        frows.append([fut_labels[i], v,
                      lower[i] if lower is not None else None,
                      upper[i] if upper is not None else None])
    tables = [
        _table("実績", [time_col, value_col],
               [[labels[i], y[i]] for i in range(len(y))]),
        _table("予測", ["期", "予測値", "下限(95%)", "上限(95%)"], frows),
    ]
    # 当てはまりの目安。原点をずらして何度も試し、平均とばらつきで示す
    if _backtest and len(y) >= 8 and not ex_cols:
        bt = _backtest_note(columns, rows, time_col, value_col,
                            method, season_length, window, exog)
        if bt["mae"] is not None:
            line = f"過去データで{bt['folds']}回試した検証（毎回{bt['hold']}期先まで予測）: "
            line += f"平均が外れた幅 = {bt['mae']:,.4g}"
            if bt["scale"]:
                line += f"（{value_col}の平均 {bt['scale']:,.4g} に対して {bt['mae'] / bt['scale'] * 100:.0f}%）"
            if bt["mape"] is not None:
                line += f"、誤差率(MAPE) = {bt['mape']:.1f}%"
                if bt["sd"] is not None:
                    line += f"（回ごとのばらつき ±{bt['sd']:.1f}ポイント）"
                line += ("。かなり当たります。" if bt["mape"] < 10 else
                         "。実用的な精度です。" if bt["mape"] < 20 else
                         "。外れやすいので参考程度に見てください。")
            notes.append(line)
            if bt["skipped"]:
                notes.append(f"※ 実績が0に近い期が {bt['skipped']} 回あり、"
                             "誤差率の計算からは外しています（割り算が跳ね上がるため）。"
                             "そういう期がある場合は、誤差率より「外れた幅」で見てください。")
    notes.append(f"予測方法: {FORECAST_METHODS[method]}。"
                 "将来は過去の延長でしか計算していません。"
                 "施策や外部要因の変化は反映されないため、幅（上限・下限）も併せて見てください。")

    return {"title": f"{value_col} の予測（{periods}期先まで）", "tables": tables,
            "notes": notes,
            "meta": {"method": method, "periods": periods,
                     "history": [_clean(v) for v in y],
                     "labels": labels,
                     "forecast": [_clean(v) for v in pred],
                     "lower": [_clean(v) for v in (lower if lower is not None else [])],
                     "upper": [_clean(v) for v in (upper if upper is not None else [])],
                     "future_labels": fut_labels}}


def timeseries(columns: list, rows: list, time_col: str, value_col: str,
               window: int = 3, season_length: int | None = None) -> dict:
    """時系列の見方をまとめる（移動平均・前期比・季節分解・自己相関）。"""
    df = _df(columns, rows)
    d = df[[time_col, value_col]].copy()
    d[value_col] = pd.to_numeric(d[value_col], errors="coerce")
    d = d.dropna().sort_values(time_col).reset_index(drop=True)
    if len(d) < 3:
        raise AnalysisError(f"時系列の分析には3点以上必要です（いま {len(d)} 点）。")
    s = d[value_col]
    out = pd.DataFrame({
        time_col: d[time_col].astype(str),
        value_col: s,
        f"移動平均({window})": s.rolling(int(window or 3), min_periods=1).mean().round(4),
        "前期比": (s.pct_change() * 100).round(2),
        "累計": s.cumsum(),
    })
    if season_length and len(s) > season_length:
        out[f"前年同期比({season_length}期前)"] = (
            (s / s.shift(int(season_length)) - 1) * 100).round(2)
    cols, rws = _out(out)
    tables = [_table("推移", cols, rws)]
    notes = []

    sl, ic, r, p, _ = stats.linregress(np.arange(len(s)), s.to_numpy())
    notes.append(f"トレンド: 1期あたり {sl:+.4g}（p値 {p:.4g}）"
                 + ("、統計的に意味のある傾きです。" if p < ALPHA
                    else "、傾きは誤差の範囲です。"))
    notes.append(f"直近値 {s.iloc[-1]:,.4g} / 平均 {s.mean():,.4g} / "
                 f"最大 {s.max():,.4g} / 最小 {s.min():,.4g}")

    if season_length and len(s) >= season_length * 2:
        from statsmodels.tsa.seasonal import seasonal_decompose
        try:
            dec = seasonal_decompose(s, model="additive", period=int(season_length))
            comp = pd.DataFrame({
                time_col: d[time_col].astype(str),
                "実績": s, "トレンド": dec.trend.round(4),
                "季節": dec.seasonal.round(4), "残差": dec.resid.round(4)})
            c2, r2 = _out(comp)
            tables.append(_table("季節分解", c2, r2))
            amp = float(dec.seasonal.max() - dec.seasonal.min())
            notes.append(f"季節変動の振れ幅 = {amp:,.4g}"
                         f"（平均の {amp / (s.mean() or 1) * 100:.1f}%）")
        except Exception as e:
            notes.append(f"季節分解はできませんでした: {e}")

    if len(s) > 4:
        lags = min(6, len(s) - 2)
        ac = [[k, float(s.autocorr(lag=k))] for k in range(1, lags + 1)]
        tables.append(_table("自己相関（何期前と似ているか）", ["ラグ", "自己相関"], ac))
        best = max(ac, key=lambda x: abs(x[1]) if not math.isnan(x[1]) else 0)
        if abs(best[1]) > 0.5:
            notes.append(f"{best[0]}期前との相関が {best[1]:.2f} と高く、"
                         f"{best[0]}期の周期がありそうです。")

    return {"title": f"{value_col} の時系列分析", "tables": tables, "notes": notes,
            "meta": {"slope": _clean(sl), "p_value": _clean(p),
                     "labels": [str(v) for v in d[time_col]],
                     "values": [_clean(v) for v in s]}}


# =============================================================================
# 外れ値（analysis.outliers の拡張）
# =============================================================================

def outliers_ext(columns: list, rows: list, target: str | list, method: str = "iqr",
                 threshold: float | None = None) -> dict:
    """外れ値の抽出。1列でも複数列（マハラノビス距離）でも扱える。"""
    if method not in OUTLIER_METHODS_EXT:
        raise AnalysisError(f"未対応の方法です: {method}。"
                            f"使えるのは {', '.join(OUTLIER_METHODS_EXT)} です。")
    df = _df(columns, rows)
    targets = [target] if isinstance(target, str) else list(target)
    notes, meta = [], {"method": method}

    if method == "mahalanobis":
        d = _num(df, targets, len(targets) + 1)
        arr = d.to_numpy(dtype=float)
        cov = np.cov(arr, rowvar=False)
        try:
            inv = np.linalg.pinv(np.atleast_2d(cov))
        except np.linalg.LinAlgError as e:
            raise AnalysisError(f"分散共分散行列を逆行列にできません: {e}") from e
        center = arr.mean(axis=0)
        md = np.sqrt(np.einsum("ij,jk,ik->i", arr - center, inv, arr - center))
        cut = float(threshold) if threshold else math.sqrt(stats.chi2.ppf(0.975, len(targets)))
        flag = md > cut
        res = df.loc[d.index].copy()
        res["距離"] = md.round(4)
        res = res[flag].sort_values("距離", ascending=False)
        notes.append(f"{len(targets)}列をまとめて見て、距離が {cut:.2f} を超えた"
                     f"{int(flag.sum())}件を外れ値としました"
                     f"（全 {len(d)}件中 {flag.mean() * 100:.1f}%）。")
    else:
        col = targets[0]
        s = _num(df, [col], 3)[col]
        if method == "iqr":
            thr = float(threshold) if threshold is not None else 1.5
            q1, q3 = s.quantile(0.25), s.quantile(0.75)
            iqr = q3 - q1
            lo, hi = q1 - thr * iqr, q3 + thr * iqr
        elif method == "zscore":
            thr = float(threshold) if threshold is not None else 3.0
            m, sd = s.mean(), s.std(ddof=1)
            lo, hi = m - thr * sd, m + thr * sd
        elif method == "modified_zscore":
            thr = float(threshold) if threshold is not None else 3.5
            med = s.median()
            mad = (s - med).abs().median()
            scale = 1.4826 * mad
            if not scale:
                raise AnalysisError("中央絶対偏差が0のため判定できません"
                                    "（同じ値ばかりです）。")
            lo, hi = med - thr * scale, med + thr * scale
        else:                                      # percentile
            thr = float(threshold) if threshold is not None else 1.0
            lo, hi = s.quantile(thr / 100), s.quantile(1 - thr / 100)
        flag = (s < lo) | (s > hi)
        res = df.loc[s.index][flag.to_numpy()].copy()
        res = res.assign(**{f"{col}_逸脱": np.where(s[flag] < lo, "下振れ", "上振れ")})
        notes.append(f"{col} の正常範囲を {lo:,.4g} 〜 {hi:,.4g} と見て、"
                     f"外れた {int(flag.sum())}件を抽出しました"
                     f"（全 {len(s)}件中 {flag.mean() * 100:.1f}%）。")
        meta.update(lower=_clean(lo), upper=_clean(hi))

    if len(res) > 500:
        notes.append(f"※ {len(res)}件のうち上位500件だけ表示します。")
        res = res.head(500)
    cols, rws = _out(res)
    meta["count"] = len(rws)
    notes.append("外れ値＝誤りとは限りません。実際に起きた特異な事象か、"
                 "入力ミスかを、元データに当たって確かめてください。")
    return {"title": f"外れ値（{OUTLIER_METHODS_EXT[method]}）",
            "tables": [_table("外れ値", cols, rws)], "notes": notes, "meta": meta}


# =============================================================================
# 分布
# =============================================================================

def distribution(columns: list, rows: list, target: str, bins: int = 20,
                 fit: list | None = None, group_col: str | None = None) -> dict:
    """分布の形を見る。ヒストグラムの度数表と、当てはまる分布の推定。"""
    df = _df(columns, rows)
    s = _num(df, [target], 3)[target]
    counts, edges = np.histogram(s.to_numpy(dtype=float), bins=int(bins or 20))
    hist = [[f"{edges[i]:,.4g} 〜 {edges[i + 1]:,.4g}", int(counts[i]),
             round(float(counts[i]) / len(s) * 100, 2)] for i in range(len(counts))]
    tables = [_table("度数分布", ["区間", "件数", "割合(%)"], hist)]
    q = s.quantile([0, .05, .25, .5, .75, .95, 1])
    tables.append(_table("要約", ["項目", "値"], [
        ["件数", len(s)], ["平均", s.mean()], ["標準偏差", s.std(ddof=1)],
        ["最小", q.iloc[0]], ["5%", q.iloc[1]], ["25%", q.iloc[2]], ["中央値", q.iloc[3]],
        ["75%", q.iloc[4]], ["95%", q.iloc[5]], ["最大", q.iloc[6]],
        ["歪度", stats.skew(s)], ["尖度", stats.kurtosis(s)]]))
    notes = []
    sk = float(stats.skew(s))
    notes.append(f"歪み: {sk:+.2f}"
                 + ("（右に長い裾。平均より中央値を見た方が実感に合います）" if sk > 0.5
                    else "（左に長い裾）" if sk < -0.5 else "（左右対称に近い）"))

    if group_col and group_col in df.columns:
        summary = df.loc[s.index].assign(**{target: s}).groupby(group_col)[target].agg(
            ["count", "mean", "median", "std", "min", "max"]).round(4)
        summary.columns = ["件数", "平均", "中央値", "標準偏差", "最小", "最大"]
        tables.append(_df_table("グループ別", summary, index_label=str(group_col)))

    fit = fit or ["norm", "lognorm"]
    frows = []
    for name in fit:
        if name not in DISTRIBUTIONS:
            continue
        dist = getattr(stats, name)
        try:
            data = s.to_numpy(dtype=float)
            if name in ("lognorm", "expon", "gamma") and (data <= 0).any():
                frows.append([DISTRIBUTIONS[name], "―", "―", "0以下の値があるため当てはめ不可"])
                continue
            params = dist.fit(data)
            ks, p = stats.kstest(data, name, args=params)
            frows.append([DISTRIBUTIONS[name], round(float(ks), 4), round(float(p), 4),
                          "当てはまる" if p >= ALPHA else "当てはまらない"])
        except Exception as e:
            frows.append([DISTRIBUTIONS[name], "―", "―", f"失敗: {e}"])
    if frows:
        tables.append(_table("分布の当てはめ（KS検定）",
                             ["分布", "KS統計量", "p値", "判定"], frows))
        ok = [r[0] for r in frows if r[3] == "当てはまる"]
        notes.append("当てはまる分布: " + ("、".join(ok) if ok else
                     "候補なし（実データ特有の形なので、シミュレーションでは"
                     "実データからの再抽出(empirical)を使ってください）"))

    return {"title": f"{target} の分布", "tables": tables, "notes": notes,
            "meta": {"bins": [_clean(e) for e in edges],
                     "counts": [int(c) for c in counts],
                     "mean": _clean(s.mean()), "std": _clean(s.std(ddof=1))}}


# =============================================================================
# シミュレーション
# =============================================================================

def _sample(rng, spec: dict, n: int, data: pd.DataFrame | None):
    """1つの入力変数のサンプルを作る。"""
    kind = (spec.get("dist") or "normal").lower()
    if kind == "empirical":
        col = spec.get("column")
        if data is None or col not in data.columns:
            raise AnalysisError(f"empirical には実データの列が要ります（{col} が見つかりません）。")
        pool = pd.to_numeric(data[col], errors="coerce").dropna().to_numpy()
        if not len(pool):
            raise AnalysisError(f"{col} に数値がありません。")
        return rng.choice(pool, size=n, replace=True)
    if kind in ("normal", "norm"):
        return rng.normal(float(spec.get("mean", 0)), abs(float(spec.get("std", 1))), n)
    if kind in ("uniform", "unif"):
        return rng.uniform(float(spec.get("min", 0)), float(spec.get("max", 1)), n)
    if kind in ("triangular", "triang"):
        lo, mode, hi = (float(spec.get("min", 0)), float(spec.get("mode", 0.5)),
                        float(spec.get("max", 1)))
        return rng.triangular(lo, min(max(mode, lo), hi), hi, n)
    if kind == "lognormal":
        return rng.lognormal(float(spec.get("mean", 0)), abs(float(spec.get("std", 1))), n)
    if kind == "poisson":
        return rng.poisson(float(spec.get("lam", 1)), n).astype(float)
    if kind == "binomial":
        return rng.binomial(int(spec.get("n", 1)), float(spec.get("p", 0.5)), n).astype(float)
    if kind in ("fixed", "const"):
        return np.full(n, float(spec.get("value", 0)))
    raise AnalysisError(f"未対応の分布です: {kind}。"
                        "normal / uniform / triangular / lognormal / poisson / "
                        "binomial / empirical / fixed から選んでください。")


_ALLOWED_FORMULA = set("0123456789.+-*/()<>=, _abcdefghijklmnopqrstuvwxyz"
                       "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _check_formula(formula: str) -> str:
    """eval に渡す前に式を検証する。

    env から __builtins__ を外すだけでは `().__class__` 経由の抜け道が残るので、
    使える文字そのものを絞る。変数名に日本語を使うため、ASCII以外の文字は
    そのまま通し（記号は Unicode の記号類だけ弾く）、`__` は禁止する。
    """
    f = (formula or "").strip()
    if not f:
        raise AnalysisError("計算式を指定してください。")
    if "__" in f:
        raise AnalysisError("計算式に `__` は使えません。")
    bad = {ch for ch in f if ch.isascii() and ch not in _ALLOWED_FORMULA}
    if bad:
        raise AnalysisError(
            f"計算式に使えない文字が入っています: {' '.join(sorted(bad))}。"
            "使えるのは 数字・変数名・+ - * / ( ) と比較記号だけです。")
    return f


def monte_carlo(formula: str, variables: dict, trials: int = 10000,
                columns: list | None = None, rows: list | None = None,
                seed: int = 0, targets: list | None = None) -> dict:
    """モンテカルロ・シミュレーション。

    formula は変数名を使った式（例: "(単価 - 原価) * 数量 - 固定費"）。
    variables は {変数名: {"dist": "normal", "mean": 100, "std": 10}} の形。
    """
    formula = _check_formula(formula)
    if not variables:
        raise AnalysisError("入力変数を1つ以上指定してください。")
    trials = max(100, min(int(trials or 10000), 200000))
    data = _df(columns, rows) if columns else None
    rng = np.random.default_rng(int(seed or 0))

    samples, spec_rows = {}, []
    for name, spec in variables.items():
        samples[name] = _sample(rng, spec or {}, trials, data)
        spec_rows.append([name, (spec or {}).get("dist", "normal"),
                          ", ".join(f"{k}={v}" for k, v in (spec or {}).items()
                                    if k != "dist")])

    # 式は numpy 配列に対して評価する。名前は変数名だけに限定する。
    env = {"__builtins__": {}, "np": np, "min": np.minimum, "max": np.maximum,
           "abs": np.abs, "where": np.where, "sqrt": np.sqrt, "log": np.log,
           "exp": np.exp}
    env.update(samples)
    try:
        result = np.asarray(eval(formula, env), dtype=float)  # noqa: S307
    except Exception as e:
        raise AnalysisError(f"計算式を評価できませんでした: {e}。"
                            f"使える変数: {', '.join(variables)}") from e
    if result.ndim == 0:
        result = np.full(trials, float(result))
    result = result[np.isfinite(result)]
    if not len(result):
        raise AnalysisError("計算結果がすべて無限大かNaNになりました。式を見直してください。")

    qs = [1, 5, 10, 25, 50, 75, 90, 95, 99]
    pct = np.percentile(result, qs)
    tables = [
        _table("入力の前提", ["変数", "分布", "パラメータ"], spec_rows),
        _table("結果の要約", ["項目", "値"], [
            ["試行回数", len(result)], ["平均", result.mean()],
            ["標準偏差", result.std(ddof=1)], ["最小", result.min()],
            ["最大", result.max()], ["中央値", np.median(result)]]),
        _table("パーセンタイル", ["区分", "値"],
               [[f"P{q}", v] for q, v in zip(qs, pct)]),
    ]
    counts, edges = np.histogram(result, bins=20)
    tables.append(_table("結果の分布", ["区間", "件数"],
                         [[f"{edges[i]:,.4g} 〜 {edges[i + 1]:,.4g}", int(counts[i])]
                          for i in range(len(counts))]))

    notes = [f"平均 {result.mean():,.4g}、"
             f"9割方 {pct[1]:,.4g} 〜 {pct[7]:,.4g} の範囲に収まります（P5〜P95）。"]
    p_neg = float((result < 0).mean())
    if p_neg > 0:
        notes.append(f"マイナスになる確率 = {p_neg * 100:.1f}%")
    for t in (targets or []):
        try:
            th = float(t)
        except (TypeError, ValueError):
            continue
        notes.append(f"{th:,.4g} を超える確率 = {float((result > th).mean()) * 100:.1f}%")

    # 感度分析。どの入力が結果を動かしているか
    sens = []
    for name, arr in samples.items():
        if np.std(arr) == 0:
            continue
        r = float(np.corrcoef(arr[:len(result)], result)[0, 1])
        sens.append([name, r, abs(r)])
    if sens:
        sens.sort(key=lambda x: -x[2])
        tables.append(_table("感度（結果との相関）", ["変数", "相関", "影響の大きさ"], sens))
        notes.append(f"結果を最も左右するのは「{sens[0][0]}」です（相関 {sens[0][1]:.2f}）。"
                     "精度を上げたいなら、まずこの変数の見積もりを詰めてください。")
    notes.append("この結果は入力の前提に完全に依存します。前提が外れれば結論も変わります。")

    return {"title": "モンテカルロ・シミュレーション", "tables": tables, "notes": notes,
            "meta": {"trials": len(result), "mean": _clean(result.mean()),
                     "percentiles": {f"P{q}": _clean(v) for q, v in zip(qs, pct)},
                     "bins": [_clean(e) for e in edges],
                     "counts": [int(c) for c in counts]}}


def scenario(formula: str, scenarios: dict, base: dict | None = None) -> dict:
    """シナリオ比較（楽観・標準・悲観など）と感度の確認。"""
    formula = _check_formula(formula)
    if not scenarios:
        raise AnalysisError("シナリオを1つ以上指定してください。")
    env_base = dict(base or {})
    names, values, rows_out = [], [], []
    keys = sorted({k for s in scenarios.values() for k in s} | set(env_base))
    for label, over in scenarios.items():
        env = {"__builtins__": {}, "min": min, "max": max, "abs": abs}
        env.update(env_base)
        env.update(over or {})
        try:
            v = float(eval(formula, env))  # noqa: S307
        except Exception as e:
            raise AnalysisError(f"シナリオ「{label}」の計算に失敗しました: {e}") from e
        names.append(label)
        values.append(v)
        rows_out.append([label] + [env.get(k) for k in keys] + [v])

    tables = [_table("シナリオ比較", ["シナリオ"] + keys + ["結果"], rows_out)]
    best, worst = max(zip(names, values), key=lambda x: x[1]), min(zip(names, values), key=lambda x: x[1])
    notes = [f"最も良いのは「{best[0]}」で {best[1]:,.4g}、"
             f"最も悪いのは「{worst[0]}」で {worst[1]:,.4g}。"
             f"振れ幅は {best[1] - worst[1]:,.4g} です。"]

    # トルネード（各変数を単独で±10%動かしたときの影響）
    if env_base:
        tor = []
        try:
            b = float(eval(formula, {"__builtins__": {}, **env_base}))  # noqa: S307
            for k, v in env_base.items():
                if not isinstance(v, (int, float)):
                    continue
                lo_env, hi_env = dict(env_base), dict(env_base)
                lo_env[k], hi_env[k] = v * 0.9, v * 1.1
                lo = float(eval(formula, {"__builtins__": {}, **lo_env}))   # noqa: S307
                hi = float(eval(formula, {"__builtins__": {}, **hi_env}))   # noqa: S307
                tor.append([k, lo, hi, abs(hi - lo)])
            tor.sort(key=lambda x: -x[3])
            if tor:
                tables.append(_table("感度（各変数を±10%動かしたとき）",
                                     ["変数", "-10%のとき", "+10%のとき", "影響幅"], tor))
                notes.append(f"基準値 {b:,.4g}。影響が大きい順に "
                             + "、".join(x[0] for x in tor[:3]) + " です。")
        except Exception:
            pass
    return {"title": "シナリオ分析", "tables": tables, "notes": notes,
            "meta": {"scenarios": {n: _clean(v) for n, v in zip(names, values)}}}


def bootstrap(columns: list, rows: list, target: str, statistic: str = "mean",
              trials: int = 5000, group_col: str | None = None,
              seed: int = 0) -> dict:
    """ブートストラップ法で統計量の信頼区間を出す。分布の仮定が要らない。"""
    funcs = {"mean": ("平均", np.mean), "median": ("中央値", np.median),
             "std": ("標準偏差", lambda a: np.std(a, ddof=1)),
             "sum": ("合計", np.sum), "p90": ("90パーセンタイル",
                                             lambda a: np.percentile(a, 90))}
    if statistic not in funcs:
        raise AnalysisError(f"未対応の統計量です: {statistic}。"
                            f"使えるのは {', '.join(funcs)} です。")
    label, fn = funcs[statistic]
    df = _df(columns, rows)
    trials = max(200, min(int(trials or 5000), 50000))
    rng = np.random.default_rng(int(seed or 0))

    def ci(arr):
        boots = np.array([fn(rng.choice(arr, size=len(arr), replace=True))
                          for _ in range(trials)])
        return fn(arr), np.percentile(boots, 2.5), np.percentile(boots, 97.5)

    out_rows = []
    if group_col and group_col in df.columns:
        d = df[[group_col, target]].copy()
        d[target] = pd.to_numeric(d[target], errors="coerce")
        for k, g in d.dropna().groupby(group_col):
            if len(g) < 3:
                continue
            v, lo, hi = ci(g[target].to_numpy(dtype=float))
            out_rows.append([str(k), len(g), v, lo, hi])
        cols = ["群", "件数", label, "95%下限", "95%上限"]
    else:
        s = _num(df, [target], 3)[target].to_numpy(dtype=float)
        v, lo, hi = ci(s)
        out_rows.append([len(s), v, lo, hi])
        cols = ["件数", label, "95%下限", "95%上限"]

    notes = [f"{trials:,}回の再抽出から求めた信頼区間です。"
             "分布の形を仮定しないので、正規分布でないデータにも使えます。"]
    if len(out_rows) > 1:
        los = [r[3] for r in out_rows]
        his = [r[4] for r in out_rows]
        overlap = not (max(los) > min(his))
        notes.append("群どうしの信頼区間が"
                     + ("重なっていません。実質的な差がありそうです。" if not overlap
                        else "重なっています。群による差は断定できません。"))
    return {"title": f"{target} の{label}と信頼区間（ブートストラップ）",
            "tables": [_table("推定", cols, out_rows)], "notes": notes,
            "meta": {"statistic": statistic, "trials": trials}}


# =============================================================================
# クラスタリング / ABC分析
# =============================================================================

def _silhouette(x: np.ndarray, labels: np.ndarray, rng) -> float:
    """シルエット係数（-1〜1。大きいほど分かれ方が良い）。

    scikit-learn は入れていないので自前で出す。全点の総当たり距離は
    件数の2乗で効いてくるため、多いときは標本を抜いて評価する。
    """
    from scipy.spatial.distance import cdist

    n = len(x)
    if n > 800:                       # 総当たりが重くなる手前で標本に切り替える
        pick = rng.choice(n, 800, replace=False)
        x, labels = x[pick], labels[pick]
        n = len(x)
    uniq = np.unique(labels)
    if len(uniq) < 2:
        return -1.0
    dist = cdist(x, x)
    scores = []
    for i in range(n):
        same = labels == labels[i]
        same[i] = False
        if not same.any():
            continue                  # 1点だけのクラスタは評価に使えない
        a = dist[i][same].mean()
        b = min(dist[i][labels == u].mean() for u in uniq if u != labels[i])
        if max(a, b) > 0:
            scores.append((b - a) / max(a, b))
    return float(np.mean(scores)) if scores else -1.0


def _cluster_naming(flat: pd.DataFrame, features: list) -> list:
    """各クラスタが「何のグループか」を、全体平均とのズレから言葉にする。

    平均値の表だけ渡されても人は読み解けない。「単価は高いが頻度は低い層」
    のように、目立つ特徴だけを拾って一言にする。
    """
    names = []
    for _, row in flat.iterrows():
        marks = []
        for f in features:
            col = flat[f"{f}の平均"]
            base, sd = col.mean(), col.std(ddof=0)
            if sd and abs(row[f"{f}の平均"] - base) >= sd * 0.8:
                marks.append(f"{f}が{'高い' if row[f'{f}の平均'] > base else '低い'}")
        names.append("・".join(marks[:3]) if marks else "平均的")
    return names


def clustering(columns: list, rows: list, features: list, k: int | str = 3,
               label_col: str | None = None, seed: int = 0,
               categorical: list | None = None) -> dict:
    """k-meansでグループ分けする（顧客や店舗のセグメント分け）。

    k に "auto" を渡すと、シルエット係数が最も高い分割数を自分で選ぶ。
    categorical に区分の列を渡すと、0/1に開いてから一緒に分ける
    （地域や会員区分のように、数値でないが効いている属性を捨てない）。
    """
    from scipy.cluster.vq import kmeans2, whiten

    df = _df(columns, rows)
    auto = str(k).lower() == "auto"
    want = 3 if auto else int(k or 3)
    d = _num(df, list(features), max(want, 3))

    parts = [d.to_numpy(dtype=float)]
    used_cat = []
    for c in (categorical or []):
        if c not in df.columns:
            raise AnalysisError(f"列が見つかりません: {c}")
        dummies = pd.get_dummies(df.loc[d.index, c].astype(str), prefix=str(c))
        if 1 < dummies.shape[1] <= 20:      # 種類が多すぎる列は分割の役に立たない
            parts.append(dummies.to_numpy(dtype=float))
            used_cat.append(c)
    arr = np.hstack(parts)
    scaled = np.nan_to_num(whiten(arr))

    rng = np.random.default_rng(int(seed or 0))
    notes = []
    if auto:
        # 2〜8グループを試して、最も素直に分かれる数を採る
        upper = max(2, min(8, len(d) // 2))
        scored = []
        for cand in range(2, upper + 1):
            try:
                _, lb = kmeans2(scaled, cand, minit="++", seed=int(seed or 0))
                if len(np.unique(lb)) < cand:
                    continue                # 空のクラスタが出た分割は採らない
                scored.append((_silhouette(scaled, lb, rng), cand))
            except Exception:
                continue
        if not scored:
            raise AnalysisError("グループ分けできませんでした。件数か列を見直してください。")
        best, k = max(scored)
        notes.append("分割数はシルエット係数（分かれ方の良さ。1に近いほど良い）で選びました: "
                     + "、".join(f"{c}群={s:.2f}" for s, c in sorted(scored, key=lambda t: t[1]))
                     + f" → {k}群を採用")
        if best < 0.25:
            notes.append("どの分割数でも係数が低く、はっきりした群には分かれていません。"
                         "この結果は「たまたまの区切り」に近いので、扱いは慎重に。")
    else:
        k = max(2, min(want, max(2, len(d) // 2)))

    centroid, labels = kmeans2(scaled, k, minit="++", seed=int(seed or 0))

    res = df.loc[d.index].copy()
    res["クラスタ"] = [f"C{i + 1}" for i in labels]
    summary = res.groupby("クラスタ")[list(features)].agg(["count", "mean"])
    flat = pd.DataFrame({"クラスタ": summary.index})
    flat["件数"] = summary[(features[0], "count")].to_numpy()
    for f in features:
        flat[f"{f}の平均"] = summary[(f, "mean")].round(4).to_numpy()
    flat.insert(1, "特徴", _cluster_naming(flat, list(features)))
    cols, rws = _out(flat)
    tables = [_table("クラスタの特徴", cols, rws)]

    show = res
    if label_col and label_col in res.columns:
        show = res[[label_col] + list(features) + ["クラスタ"]]
    if len(show) > 500:
        show = show.head(500)
    c2, r2 = _out(show)
    tables.append(_table("割り当て（先頭500件）", c2, r2))

    sizes = flat["件数"].tolist()
    notes.append(f"{k}グループに分けました（件数: {', '.join(map(str, sizes))}）。")
    notes.append("各列は尺度が違うと結果が歪むため、標準化してから分けています。")
    if used_cat:
        notes.append(f"区分の列も0/1に開いて使いました: {', '.join(used_cat)}")
    for _, row in flat.iterrows():
        notes.append(f"{row['クラスタ']}（{row['件数']}件）: {row['特徴']}")
    return {"title": f"クラスタ分析（{k}グループ）", "tables": tables, "notes": notes,
            "meta": {"k": int(k), "sizes": [int(s) for s in sizes]}}


def abc_analysis(columns: list, rows: list, label_col: str, value_col: str,
                 thresholds: list | None = None) -> dict:
    """ABC分析（パレート）。売上の8割を占める品目を切り出す。"""
    df = _df(columns, rows)
    if label_col not in df.columns:
        raise AnalysisError(f"列が見つかりません: {label_col}")
    d = df[[label_col, value_col]].copy()
    d[value_col] = pd.to_numeric(d[value_col], errors="coerce")
    d = d.dropna().groupby(label_col, as_index=False)[value_col].sum()
    if d.empty:
        raise AnalysisError("集計できる行がありません。")
    d = d.sort_values(value_col, ascending=False).reset_index(drop=True)
    total = d[value_col].sum()
    d["構成比(%)"] = (d[value_col] / total * 100).round(2)
    d["累計構成比(%)"] = d["構成比(%)"].cumsum().round(2)
    cuts = thresholds or [70, 90]
    d["区分"] = np.where(d["累計構成比(%)"] <= cuts[0], "A",
                         np.where(d["累計構成比(%)"] <= cuts[1], "B", "C"))
    cols, rws = _out(d)
    summary = d.groupby("区分").agg(品目数=(label_col, "count"),
                                   金額=(value_col, "sum")).reset_index()
    summary["金額構成比(%)"] = (summary["金額"] / total * 100).round(2)
    summary["品目構成比(%)"] = (summary["品目数"] / len(d) * 100).round(2)
    c2, r2 = _out(summary)

    a = d[d["区分"] == "A"]
    notes = [f"全 {len(d)} 品目のうち、上位 {len(a)} 品目"
             f"（{len(a) / len(d) * 100:.1f}%）で {value_col} の "
             f"{a['構成比(%)'].sum():.1f}% を占めます。"]
    if len(a):
        notes.append("A区分: " + "、".join(map(str, a[label_col].head(10))))
    return {"title": f"ABC分析（{value_col}）",
            "tables": [_table("区分の要約", c2, r2), _table("明細", cols, rws)],
            "notes": notes,
            "meta": {"labels": [str(v) for v in d[label_col]],
                     "values": [_clean(v) for v in d[value_col]],
                     "cumulative": [_clean(v) for v in d["累計構成比(%)"]]}}


# ==========================================================================
# ===== 元 analysis.py
# SQLiteだけでは書けない集計・統計を pandas で行う。
#
# このアプリのSQLite(3.32)には次が無い:
#   STDDEV / VARIANCE / MEDIAN / CORR / PERCENTILE / SQRT / POWER / PIVOT構文
# そのため「相関」「中央値」「ばらつき」「クロス集計」はSQLでは実質書けない。
# ここではSELECT結果(columns, rows)を受け取り、同じ形(columns, rows)で返す。
# ==========================================================================
import math

import numpy as np
import pandas as pd

AGG_FUNCS = {
    "sum": ("合計", "sum"),
    "mean": ("平均", "mean"),
    "count": ("件数", "count"),
    "median": ("中央値", "median"),
    "min": ("最小", "min"),
    "max": ("最大", "max"),
    "std": ("標準偏差", "std"),
    "nunique": ("種類数", "nunique"),
}
CORR_METHODS = ("pearson", "spearman")
OUTLIER_METHODS = ("iqr", "zscore")
MARGIN_NAME = "合計"


def _df(columns: list, rows: list) -> pd.DataFrame:
    return pd.DataFrame([list(r) for r in rows], columns=list(columns))


def _to_numeric(df: pd.DataFrame, cols) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def numeric_columns(columns: list, rows: list) -> list:
    """数値として扱える列を推定する（8割以上が数値なら数値列とみなす）。"""
    df = _df(columns, rows)
    out = []
    for c in df.columns:
        s = pd.to_numeric(df[c], errors="coerce")
        if len(s) and s.notna().mean() >= 0.8:
            out.append(c)
    return out


def _clean(v):
    """JSONに載せられる形へ（NaN/Infとnumpy型を素の値にする）。"""
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 6)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def _out(df: pd.DataFrame):
    """DataFrame を (columns, rows) に戻す。"""
    cols = [str(c) for c in df.columns]
    rows = [tuple(_clean(v) for v in r) for r in df.itertuples(index=False, name=None)]
    return cols, rows


# --- クロス集計 -----------------------------------------------------------------

def _sorted_by(pt: pd.DataFrame, rank_by: str) -> pd.DataFrame:
    """大きい順に並べ替える。rank_by が列名ならその列、それ以外は行の合計で。"""
    target = None
    for c in pt.columns:
        if str(c) == str(rank_by):
            target = c
            break
    key = pt[target] if target is not None else pt.sum(axis=1, numeric_only=True)
    return pt.loc[key.sort_values(ascending=False).index]


def _as_percent(pt: pd.DataFrame, mode: str) -> pd.DataFrame:
    """実数を構成比(%)に置き換える。合計が0の行や列は0のままにする。"""
    num = pt.select_dtypes("number")
    if mode == "row":
        denom = num.sum(axis=1).replace(0, np.nan)
        out = num.div(denom, axis=0)
    elif mode == "column":
        denom = num.sum(axis=0).replace(0, np.nan)
        out = num.div(denom, axis=1)
    else:
        total = num.to_numpy().sum()
        out = num / (total if total else np.nan)
    pt = pt.copy()
    pt[num.columns] = (out * 100).round(1).fillna(0.0)
    return pt


#: 構成比の取り方。クロス集計は「実数で見たい」より「割合で見たい」ことが多い。
PERCENT_MODES = {
    "row": "行内の構成比（行ごとに合計100%）",
    "column": "列内の構成比（列ごとに合計100%）",
    "total": "全体に対する構成比（表全体で100%）",
}


def pivot(columns: list, rows: list, index: list, cols: str | None, values: str,
          aggfunc: str = "sum", fill_value=0, margins: bool = False,
          percent: str | None = None, rank_by: str | None = None):
    """クロス集計表を作る。SQLiteにPIVOT構文が無いのでここで行う。

    index   : 行にする列（複数可）
    cols    : 列に展開する列（省略可。省略時は index ごとの集計表になる）
    values  : 集計する値の列
    percent : row / column / total を指定すると実数を構成比(%)に置き換える
    rank_by : 指定した列（または合計）の大きい順に並べ、順位の列を先頭に足す
    """
    if not index:
        raise ValueError("index（行にする列）を1つ以上指定してください。")
    if not values:
        raise ValueError("values（集計する値の列）を指定してください。")
    if aggfunc not in AGG_FUNCS:
        raise ValueError(f"aggfunc は {', '.join(AGG_FUNCS)} のいずれかです。")
    if percent and percent not in PERCENT_MODES:
        raise ValueError(f"percent は {', '.join(PERCENT_MODES)} のいずれかです。")

    df = _df(columns, rows)
    missing = [c for c in list(index) + ([cols] if cols else []) + [values]
               if c not in df.columns]
    if missing:
        raise ValueError(f"指定列が結果にありません: {missing} / 利用可能: {list(df.columns)}")

    if aggfunc not in ("count", "nunique"):
        _to_numeric(df, [values])

    pt = pd.pivot_table(
        df, index=list(index), columns=cols, values=values,
        aggfunc=AGG_FUNCS[aggfunc][1],
        fill_value=fill_value, margins=margins and not percent,
        margins_name=MARGIN_NAME, dropna=False, observed=False,
    )
    if isinstance(pt, pd.Series):
        pt = pt.to_frame(name=values)

    # 並べ替えは構成比にする前に行う（%にすると行内の大小が消えることがある）
    if rank_by:
        pt = _sorted_by(pt, rank_by)
    if percent:
        pt = _as_percent(pt, percent)

    pt = pt.reset_index()
    if rank_by:
        pt.insert(0, "順位", range(1, len(pt) + 1))
    # 列がMultiIndex（valuesとcolsの2段）になる場合があるので平坦化する
    flat = []
    for c in pt.columns:
        if isinstance(c, tuple):
            parts = [str(p) for p in c if str(p) != ""]
            flat.append(" / ".join(parts) if parts else values)
        else:
            flat.append(str(c))
    pt.columns = flat
    return _out(pt)


# --- 基本統計量 -----------------------------------------------------------------

_DESC_LABELS = {"count": "件数", "mean": "平均", "std": "標準偏差", "min": "最小",
                "25%": "25%", "50%": "中央値", "75%": "75%", "max": "最大"}


def describe(columns: list, rows: list, targets: list | None = None,
             group_by: str | None = None):
    """基本統計量（件数/平均/標準偏差/最小/四分位/中央値/最大）。"""
    df = _df(columns, rows)
    targets = list(targets or []) or numeric_columns(columns, rows)
    targets = [c for c in targets if c in df.columns and c != group_by]
    if not targets:
        raise ValueError("数値として集計できる列がありません。columns で対象列を指定してください。")
    _to_numeric(df, targets)

    if group_by:
        if group_by not in df.columns:
            raise ValueError(f"group_by の列 '{group_by}' が結果にありません。")
        out = []
        for key, g in df.groupby(group_by, dropna=False):
            d = g[targets].describe().T.reset_index().rename(columns={"index": "列"})
            d.insert(0, group_by, key)
            out.append(d)
        res = pd.concat(out, ignore_index=True)
    else:
        res = df[targets].describe().T.reset_index().rename(columns={"index": "列"})

    res = res.rename(columns=_DESC_LABELS)
    for c in res.columns:
        if c not in ("列", group_by):
            res[c] = pd.to_numeric(res[c], errors="coerce").round(3)
    return _out(res)


# --- 相関 -----------------------------------------------------------------------

def correlation(columns: list, rows: list, targets: list | None = None,
                method: str = "pearson"):
    """数値列どうしの相関行列。1列目が列名なので、そのままヒートマップにできる。"""
    if method not in CORR_METHODS:
        raise ValueError(f"method は {', '.join(CORR_METHODS)} のいずれかです。")
    df = _df(columns, rows)
    targets = list(targets or []) or numeric_columns(columns, rows)
    targets = [c for c in targets if c in df.columns]
    if len(targets) < 2:
        raise ValueError("相関には数値列が2つ以上必要です。columns で対象列を指定してください。")
    _to_numeric(df, targets)
    corr = df[targets].corr(method=method).round(3).reset_index()
    corr = corr.rename(columns={"index": "列"})
    return _out(corr)


def correlation_pairs(columns: list, rows: list, method: str = "pearson"):
    """相関の強い組み合わせを、強さ順のリストで返す（LLMへの説明用）。"""
    cols, mrows = correlation(columns, rows, None, method)
    names = cols[1:]
    pairs = []
    for i, r in enumerate(mrows):
        for j, v in enumerate(r[1:]):
            if j > i and v is not None:
                pairs.append({"a": names[i], "b": names[j], "corr": v})
    pairs.sort(key=lambda p: abs(p["corr"]), reverse=True)
    return pairs


# --- 外れ値 ---------------------------------------------------------------------

def outliers(columns: list, rows: list, target: str, method: str = "iqr",
             threshold: float = 1.5, limit: int = 200):
    """外れ値の行を抜き出す。戻り値: (columns, rows, 判定に使った情報)"""
    if method not in OUTLIER_METHODS:
        raise ValueError(f"method は {', '.join(OUTLIER_METHODS)} のいずれかです。")
    df = _df(columns, rows)
    if target not in df.columns:
        raise ValueError(f"target の列 '{target}' が結果にありません。利用可能: {list(df.columns)}")
    _to_numeric(df, [target])
    s = df[target].dropna()
    if s.empty:
        raise ValueError(f"'{target}' に数値がありません。")

    if method == "iqr":
        q1, q3 = float(s.quantile(0.25)), float(s.quantile(0.75))
        iqr = q3 - q1
        lo, hi = q1 - threshold * iqr, q3 + threshold * iqr
        info = {"方式": f"IQR×{threshold}", "Q1": round(q1, 3), "Q3": round(q3, 3),
                "下限": round(lo, 3), "上限": round(hi, 3)}
        mask = (df[target] < lo) | (df[target] > hi)
    else:
        mu, sd = float(s.mean()), float(s.std(ddof=0))
        lo, hi = mu - threshold * sd, mu + threshold * sd
        info = {"方式": f"Zスコア±{threshold}", "平均": round(mu, 3),
                "標準偏差": round(sd, 3), "下限": round(lo, 3), "上限": round(hi, 3)}
        mask = (df[target] < lo) | (df[target] > hi) if sd > 0 else pd.Series(False, index=df.index)

    hit = df[mask.fillna(False)].copy()
    info["全体件数"] = int(len(df))
    info["外れ値件数"] = int(len(hit))
    info["割合(%)"] = round(100.0 * len(hit) / len(df), 2) if len(df) else 0.0
    hit = hit.sort_values(target, ascending=False).head(limit)
    c, r = _out(hit)
    return c, r, info


# 統合前と同じく `import analysis` と書けるようにする。
# （ここより上の行番号を動かさないよう、末尾に置いている）
import sys as _sys
_sys.modules["analysis"] = _sys.modules[__name__]
