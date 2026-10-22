from taskcost.user_profile import (
    arabic_share, build_profile, design_context, forget, stated_monthly_volume, use_case_prior, valid_client,
)
from taskcost.usage import load_events, log_event

ME = "client-0001-abcd"


def request(**fields):
    return {"kind": "request", "via": "start", "client": ME, "at": "2026-10-22T10:00:00+00:00",
            "domain": "logistics", "arabic_share": 0.8, "country": "EG", "sovereignty": "prefer_local", **fields}


def comparison(use_case, trials=1, rerun=False):
    return {"kind": "comparison_done", "client": ME, "at": "2026-10-22T10:00:01+00:00", "use_case": use_case,
            "trials": trials, "rerun": rerun}


def test_volumes_language_and_ids_are_inferred_from_the_request():
    assert stated_monthly_volume("We get about 500 invoices a month") == 500
    assert stated_monthly_volume("2k customer emails per day") == 44_000
    assert stated_monthly_volume("Write an email") is None
    assert arabic_share("اكتب رسالة للعميل") > 0.9 and arabic_share("Write an email") == 0
    assert valid_client(ME) == ME and valid_client("<script>") == "" and valid_client("") == ""


def test_profile_learns_industry_language_usual_use_case_and_scale():
    events = [request(monthly_volume=1200), request(), request(),
              comparison("batch"), comparison("chat", trials=3), comparison("chat", trials=3),
              {"kind": "use_case_changed", "client": ME, "at": "x", "from_use_case": "batch", "to_use_case": "chat"},
              {"kind": "request", "via": "start", "client": "someone-else-123", "at": "x", "domain": "healthcare"}]
    p = build_profile(ME, events)
    assert p["domain"] == "logistics" and p["language"] == "Arabic" and p["monthly_volume"] == 1200
    assert p["usual_use_case"] == "chat" and p["prefers_reliability"] and p["tailoring"]
    context = design_context(p)
    assert "logistics" in context and "Arabic" in context
    assert build_profile("nobody-1234567", events) == {"client": "nobody-1234567", "requests": 0, "comparisons": 0}


def test_unclear_use_case_follows_the_usual_one_but_a_clear_guess_wins():
    p = {"tailoring": True, "usual_use_case": "chat"}
    assert use_case_prior(p, "batch", {"batch": 0.5, "chat": 0.4})[0] == "chat"
    assert use_case_prior(p, "batch", {"batch": 0.9})[0] == "batch"
    assert use_case_prior({"tailoring": False, "usual_use_case": "chat"}, "batch", {"batch": 0.4})[0] == "batch"


def test_forget_deletes_only_this_users_events():
    log_event("request", client=ME, via="start")
    log_event("request", client="other-client-99", via="start")
    assert forget(ME) == 1
    assert [e["client"] for e in load_events()] == ["other-client-99"]


def test_volumes_are_read_in_arabic_too():
    assert stated_monthly_volume("نشحن حوالي 3000 شحنة في الشهر") == 3000
    assert stated_monthly_volume("٥٠٠ فاتورة شهريا") == 500
    assert stated_monthly_volume("10 ألف رسالة كل يوم") == 220_000


def test_industry_adds_up_probabilities_across_requests():
    shipping = request(domain="logistics", domain_scores={"logistics": 0.6, "retail_ecommerce": 0.3})
    complaints = request(domain="retail_ecommerce", domain_scores={"retail_ecommerce": 0.5, "logistics": 0.45})
    vague = request(domain="general", domain_scores={"general": 0.9})
    assert build_profile(ME, [shipping, complaints, vague])["domain"] == "logistics"
    mixed = [request(domain_scores={"healthcare": 0.5}), request(domain_scores={"legal": 0.5}),
             request(domain_scores={"telecom": 0.5})]
    assert "domain" not in build_profile(ME, mixed)  # no clear industry yet
