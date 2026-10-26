"""ClinicalTrials.gov API v2。スポンサー名や候補名で治験の進展を集める。

published は最終更新日 (LastUpdatePostDate) を使う。治験は更新が疎なので、
sources.json 側で lookback_days を長め (365) に設定する想定。
"""

import json
from datetime import timedelta
from ..http import http_fetch


URL = "https://clinicaltrials.gov/api/v2/studies"


def _mod(study, *names):
    ps = study.get("protocolSection") or {}
    for n in names:
        m = ps.get(n)
        if isinstance(m, dict):
            return m
    return {}


def parse_studies(data):
    out = []
    for study in data.get("studies") or []:
        idm = _mod(study, "identificationModule")
        nct = (idm.get("nctId") or "").strip()
        if not nct:
            continue
        desc = _mod(study, "descriptionModule")
        status = _mod(study, "statusModule")
        design = _mod(study, "designModule")
        cond = _mod(study, "conditionsModule")
        arms = _mod(study, "armsInterventionsModule")
        sponsor = ((_mod(study, "sponsorCollaboratorsModule").get("leadSponsor") or {}).get("name") or "").strip()
        parts = [desc.get("briefSummary") or "", desc.get("detailedDescription") or ""]
        meta = []
        if status.get("overallStatus"):
            meta.append(f"Status: {status['overallStatus']}")
        phases = design.get("phases") or []
        if phases:
            meta.append("Phase: " + ", ".join(phases))
        if sponsor:
            meta.append(f"Sponsor: {sponsor}")
        abstract = " ".join(p.strip() for p in parts if p and p.strip())
        if meta:
            abstract = (abstract + " " + ". ".join(meta) + ".").strip()
        fields = list(cond.get("conditions") or [])
        fields += [i.get("name") for i in arms.get("interventions") or [] if i.get("name")]
        updated = ((status.get("lastUpdatePostDateStruct") or {}).get("date") or
                   (status.get("startDateStruct") or {}).get("date"))
        out.append({
            "source": "trials", "source_id": nct,
            "title": (idm.get("officialTitle") or idm.get("briefTitle") or "").strip(),
            "abstract": abstract,
            "url": f"https://clinicaltrials.gov/study/{nct}",
            "published": updated,
            "fields": fields[:10],
            "affiliations": [sponsor] if sponsor else [],
            "info_type": "trial",
        })
    return out


def trials_fetch(query, max_results, lookback_days, as_of, http=None):
    http = http or http_fetch
    raw = http(URL, params={"query.term": query, "pageSize": max_results,
                            "format": "json", "sort": "LastUpdatePostDate:desc"})
    items = parse_studies(json.loads(raw))
    start = (as_of - timedelta(days=lookback_days)).isoformat()
    items = [r for r in items if r["published"] and start <= r["published"] <= as_of.isoformat()]
    return items[:max_results]
