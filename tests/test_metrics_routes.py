from metrics import normalize_route


def test_dynamic_listing_paths_never_use_real_slug_as_metric_label():
    slug = "private-host-secret-slug"
    expected = {
        "/services/%s" % slug: "/services/:slug",
        "/mcp/servers/%s" % slug: "/mcp/servers/:slug",
        "/a2a/agents/%s" % slug: "/a2a/agents/:slug",
        "/listings/x402/%s/edit" % slug: "/listings/:kind/:slug/edit",
        "/api/v1/services/%s" % slug: "/api/v1/services/:slug",
        "/api/v1/mcp/servers/%s" % slug: "/api/v1/mcp/servers/:slug",
        "/api/v1/a2a/agents/%s" % slug: "/api/v1/a2a/agents/:slug",
        "/api/v1/listings/x402/%s" % slug: "/api/v1/listings/:kind/:slug",
    }
    assert {path: normalize_route(path) for path in expected} == expected


def test_unknown_known_prefix_paths_are_collapsed():
    labels = {normalize_route("/api/v1/probe-%d" % index)
              for index in range(20000)}
    assert labels == {"/api/v1/*"}


def test_unknown_http_methods_are_bucketed():
    from metrics import _HTTP_METHODS
    methods = {value if value in _HTTP_METHODS else "_other"
               for value in ("GET", *("METHOD%d" % i for i in range(1000)))}
    assert methods == {"GET", "_other"}