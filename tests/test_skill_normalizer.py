"""Phase 1: canonical skill normalization."""
from src.engine.skill_normalizer import (
    canonical_name,
    normalize_skill,
    normalize_skills,
)


def test_exact_and_case_insensitive():
    assert normalize_skill("PySpark") == "pyspark"
    assert normalize_skill("PYSPARK") == "pyspark"
    assert normalize_skill("pyspark") == "pyspark"


def test_alias_resolution():
    assert normalize_skill("k8s") == "kubernetes"
    assert normalize_skill("K8s") == "kubernetes"
    assert normalize_skill("SSIS packages") == "ssis"
    assert normalize_skill("DataStage jobs") == "datastage"
    assert normalize_skill("Postgres") == "postgres"
    assert normalize_skill("TF") == "terraform"
    assert normalize_skill("Airflow DAGs") == "airflow"


def test_fuzzy_typo():
    assert normalize_skill("Kubernates") == "kubernetes"
    assert normalize_skill("Pyspar") == "pyspark"


def test_unknown_returns_none_not_dropped_silently():
    assert normalize_skill("QuantumFluxDB") is None


def test_batch_and_canonical_name():
    out = normalize_skills(["PySpark", "k8s", "Nope"])
    assert out == {"PySpark": "pyspark", "k8s": "kubernetes", "Nope": None}
    assert canonical_name("pyspark") == "PySpark"
    assert canonical_name("datastage") == "IBM DataStage"


def test_bank_tags_all_resolve():
    """Every skill tag used in the framework bullet banks must normalize."""
    import glob
    import json

    unresolved = set()
    for f in glob.glob("config/frameworks/*.json"):
        if "registry" in f:
            continue
        for b in json.load(open(f)).get("bullet_bank", []):
            for s in b.get("skills", []):
                if normalize_skill(s) is None:
                    unresolved.add(s)
    assert not unresolved, f"bank tags with no canonical id: {sorted(unresolved)}"
