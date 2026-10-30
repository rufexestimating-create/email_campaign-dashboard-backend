"""Live check of every milestone's API against the real Supabase project. Sends no email (run from backend/)."""
import os
import sys

sys.path.insert(0, os.getcwd())

from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.db.supabase import get_supabase
from app.main import app
from app.services import follow_ups

uid = get_supabase().table("users").select("id").eq("email", "hamzaakram53454@gmail.com").execute().data[0]["id"]
app.dependency_overrides[get_current_user] = lambda: {"id": uid}
c = TestClient(app, raise_server_exceptions=False)
ok = True


def check(milestone, name, passed, detail=""):
    global ok
    ok &= bool(passed)
    print(f"  [{'PASS' if passed else 'FAIL'}] {milestone:4} {name}{(' - ' + str(detail)) if detail else ''}")


spammy = {"subject": "FREE QUOTE!!! act now", "body": "Get your FREE QUOTE TODAY!! https://bit.ly/x $99", "content_type": "text"}
r = c.post("/api/v1/template/score", json=spammy).json()
check("M1", "rules scorer: score, reasons, highlight positions, fixes", r["rule_score"] >= 45 and r["reasons"] and r["highlights"] and any(i["fix"] for i in r["issues"]), f"rules {r['rule_score']}")
check("M2", "editor data: every highlight points into the text", all(0 <= h["start"] < h["end"] <= len(spammy[h["field"]]) for h in r["highlights"]))
s = c.get("/api/v1/insights/tracking-summary")
check("M3", "tracking summary + leads table reachable", s.status_code == 200 and c.get("/api/v1/leads").status_code == 200, s.json()["events"])
check("M3", "webhook refuses unsigned events", c.post("/api/v1/webhooks/resend", json={"type": "email.clicked"}).status_code in (401, 503))
m = c.get("/api/v1/insights/model").json()
check("M4", "spam model info + calibration plot", m["available"] and c.get("/api/v1/insights/model/calibration.png").status_code == 200, f"v{m['version']} ROC-AUC {m['metrics']['roc_auc']}")
check("M5", "score = rules + model, with model reasons", r["model"] and r["model"]["reasons"] and r["score"] != r["rule_score"] or r["score"] == 100, f"final {r['score']}, model {r['model']['spam_percent']}%")
st = c.get("/api/v1/insights/send-time?timezone=America/New_York")
plan = c.post("/api/v1/insights/send-plan", json={"recipients": ["hamzaakram53454@gmail.com"], "timezone": "America/New_York"})
check("M6", "heatmaps, windows, send plan", st.status_code == 200 and plan.status_code == 200 and "send_at_utc" in plan.json()[0], plan.json()[0]["window"])
pf = c.post("/api/v1/campaign/preflight", json={"subject": "Kitchen ideas", "body": "Hi {{first_name}}, ideas for your kitchen.",
                                                "recipients": ["hamzaakram53454@gmail.com", "info@somefirm.com"], "from_email": "hello@arvistry.com",
                                                "schedule": {"type": "now", "timezone": "America/New_York"}}).json()
check("M7", "preflight runs all 8 checks", len(pf["checks"]) == 8 and pf["verdict"] in ("pass", "warn", "fail"), f"{pf['verdict']} {pf['counts']}")
auth = next(x for x in pf["checks"] if x["id"] == "authentication")
check("M7", "live DNS: arvistry.com SPF / DKIM / DMARC", auth["status"] == "pass", auth["message"])
d = c.post("/api/v1/campaign/preflight/decisions", json={"verdict": "fail", "checks": [{"id": "x", "status": "fail", "message": "m"}], "accepted": [], "outcome": "sent"})
check("M8", "override without a reason is refused; log readable", d.status_code == 422 and c.get("/api/v1/campaign/preflight/decisions").status_code == 200)
sc = c.post("/api/v1/leads/score").json()
ov = c.get("/api/v1/leads/scores").json()
check("M9", "leads re-scored with the calibrated model", sc["scored"] == len(ov["leads"]) and all(l["p_sql"] is not None for l in ov["leads"]),
      f"{sc['scored']} leads, model {sc['model_version']}, bands {ov['bands']}")
camps = c.get("/api/v1/campaigns").json()
fd = c.get(f"/api/v1/campaigns/{camps[0]['id']}/follow-ups")
check("M10", "follow-up counts on campaigns, detail endpoint, scheduler pass", all("follow_ups" in x for x in camps) and fd.status_code == 200
      and follow_ups.run_due() == {"sent": 0, "skipped": 0, "failed": 0})

print("\nRESULT:", "ALL LIVE CHECKS PASSED" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
