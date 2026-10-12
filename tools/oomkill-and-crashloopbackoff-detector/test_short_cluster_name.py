"""Unit tests for short_cluster_name (ROSA e01/i01 and classic 3-segment ids)."""

from oom_cluster import short_cluster_name


def test_kflux_c_prd_e01_and_i01_are_distinct():
    e01 = "default/api-kflux-c-prd-e01-yo5u-p3-openshiftapps-com:443/smodak"
    i01 = "default/api-kflux-c-prd-i01-7hyu-p3-openshiftapps-com:443/smodak"
    assert short_cluster_name(e01) == "kflux-c-prd-e01"
    assert short_cluster_name(i01) == "kflux-c-prd-i01"
    assert short_cluster_name(e01) != short_cluster_name(i01)


def test_kflux_c_stg_e01():
    ctx = "/api-kflux-c-stg-e01-3zdg-p3-openshiftapps-com:443/system:serviceaccount:x:y"
    assert short_cluster_name(ctx) == "kflux-c-stg-e01"


def test_classic_three_segment_names_unchanged():
    samples = {
        "default/api-stone-prod-p02-hjvn-p1-openshiftapps-com:6443/smodak": "stone-prod-p02",
        "default/api-stone-prd-rh01-pg1f-p1-openshiftapps-com:6443/smodak": "stone-prd-rh01",
        "default/api-stone-stg-rh01-l2vh-p1-openshiftapps-com:6443/smodak": "stone-stg-rh01",
        "default/api-kflux-prd-rh02-abcd-p1-openshiftapps-com:6443/smodak": "kflux-prd-rh02",
        "default/api-stone-stage-p01-hpmt-p1-openshiftapps-com:6443/smodak": "stone-stage-p01",
    }
    for ctx, expected in samples.items():
        assert short_cluster_name(ctx) == expected, ctx


def test_dns_api_url_form():
    url = "https://api.kflux-c-prd-i01.7hyu.p3.openshiftapps.com:443"
    assert short_cluster_name(url) == "kflux-c-prd-i01"
