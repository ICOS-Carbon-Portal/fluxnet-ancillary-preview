"""Parse an ICOS ETC L2 ancillary BIF file into tidy + per-group Parquet tables.

Input (from the L2 ARCHIVE zip, extracted via the Carbon Portal):
  ICOSETC_<site>_ANCILLARY_L2.csv   - BADM long format: SITE_ID, GROUP_ID,
                                      VARIABLE_GROUP, VARIABLE, DATAVALUE
  BIF_Ancillary_Variables.csv       - variable dictionary (name, description, unit)

Output (d:/agent-test/parquet/):
  ancillary_long.parquet            - one tidy long table (all groups)
  groups/<GROUP>.parquet            - one wide table per variable group
  variable_dictionary.parquet       - variable -> description, unit

This replaces the R suite's manual Colab "BIF -> BIFTAB" pivot step.
"""
from pathlib import Path
import pandas as pd

HERE = Path(r"d:\agent-test")
OUT = HERE / "parquet"


def _read_csv(path: Path, **kw) -> pd.DataFrame:
    """Read CSV, tolerating the Latin-1 bytes (e.g. degree sign) ICOS files contain."""
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return pd.read_csv(path, encoding=enc, **kw)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, encoding="latin-1", encoding_errors="replace", **kw)


def load_dictionary(path: Path) -> pd.DataFrame:
    d = _read_csv(path)
    d.columns = ["VARIABLE", "DESCRIPTION", "UNIT"][: len(d.columns)]
    d["VARIABLE"] = d["VARIABLE"].str.strip()
    return d.drop_duplicates("VARIABLE").reset_index(drop=True)


def load_bif(path: Path) -> pd.DataFrame:
    bif = _read_csv(path, dtype=str)
    bif.columns = [c.strip().strip('"') for c in bif.columns]
    for c in bif.columns:
        bif[c] = bif[c].str.strip()
    return bif


def tidy_long(bif: pd.DataFrame) -> pd.DataFrame:
    """Canonical tidy long frame, ready for groupby/pivot by anyone."""
    return (bif.rename(columns={"SITE_ID": "SITE", "VARIABLE_GROUP": "GROUP",
                                "DATAVALUE": "VALUE"})
               [["SITE", "GROUP", "GROUP_ID", "VARIABLE", "VALUE"]])


def group_wide(bif: pd.DataFrame, group: str) -> pd.DataFrame:
    """One row per record (GROUP_ID) of a variable group, columns = its VARIABLEs."""
    sub = bif[bif.VARIABLE_GROUP == group]
    w = (sub.pivot_table(index=["SITE_ID", "GROUP_ID"], columns="VARIABLE",
                         values="DATAVALUE", aggfunc="first")
            .reset_index())
    w.columns.name = None

    # NB: values are kept as strings here (categorical bases like SOIL_WRB_GROUP
    # would be destroyed by blanket numeric coercion); consumers coerce as needed.

    # derive YEAR from the group's date column, if any
    date_col = next((c for c in w.columns if c.endswith("_DATE")), None) \
        or next((c for c in w.columns if c.endswith("_DATE_START")), None)
    if date_col:
        w["YEAR"] = w[date_col].str.slice(0, 4)
    return w


def export(bif_path: Path, dict_path: Path, out: Path = OUT) -> None:
    bif = load_bif(bif_path)
    dic = load_dictionary(dict_path)
    site = bif.SITE_ID.iloc[0]
    (out / "groups").mkdir(parents=True, exist_ok=True)

    tidy_long(bif).to_parquet(out / "ancillary_long.parquet", index=False)
    dic.to_parquet(out / "variable_dictionary.parquet", index=False)

    print(f"site {site}: {len(bif):,} values, {bif.VARIABLE_GROUP.nunique()} groups")
    print(f"  -> ancillary_long.parquet ({len(bif):,} rows)")
    print(f"  -> variable_dictionary.parquet ({len(dic):,} variables)")
    for grp in sorted(bif.VARIABLE_GROUP.unique()):
        w = group_wide(bif, grp)
        w.to_parquet(out / "groups" / f"{grp}.parquet", index=False)
        print(f"  -> groups/{grp}.parquet  ({len(w):,} records x {w.shape[1]} cols)")


if __name__ == "__main__":
    export(HERE / "ICOSETC_BE-Bra_ANCILLARY_L2.csv",
           HERE / "BE-Bra_BIF_Ancillary_Variables.csv")
