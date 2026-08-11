"""L'agent doit pouvoir choisir LIBREMENT symbole, timeframe, indicateur et news."""
import _isolation  # noqa: F401  (base SQLite temporaire)
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data import chart, indicators
from brain import tools as T


def _df(n: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1.10 + np.cumsum(rng.normal(0.0002, 0.002, n))
    high = close + abs(rng.normal(0.001, 0.0005, n))
    low = close - abs(rng.normal(0.001, 0.0005, n))
    return pd.DataFrame({
        "time": pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC"),
        "open": np.r_[close[0], close[:-1]], "high": high, "low": low, "close": close,
        "tick_volume": rng.integers(100, 1000, n),
    })


class FakeProvider:
    """Orchestrateur simule (interface bind_live)."""
    def __init__(self):
        self.df = _df()
        self.calls = []

    def symbols(self, query="", only_watchlist=False, limit=40):
        self.calls.append(("symbols", query))
        univers = [{"symbol": s, "groupe": "Forex", "description": "", "spread_points": 8,
                    "digits": 5, "dans_market_watch": True}
                   for s in ("EURUSD", "AUDNZD", "XAUUSD", "US30")]
        return [u for u in univers if not query or query.upper() in u["symbol"]][:limit]

    def market(self, symbol, timeframe=None):
        self.calls.append(("market", symbol, timeframe))
        from data.market import snapshot
        return snapshot(symbol, self.df, {"digits": 5})

    def chart(self, symbol, timeframes, candles=10):
        self.calls.append(("chart", symbol, tuple(timeframes), candles))
        tfs = list(timeframes) or ["W1", "D1", "H4"]      # idem run.Orchestrator.chart
        return chart.read(symbol, {tf: self.df for tf in tfs}, {"digits": 5}, candles=candles)

    def indicator(self, symbol, timeframe, name, params):
        self.calls.append(("indicator", symbol, timeframe, name, dict(params)))
        return {"symbol": symbol, "timeframe": timeframe,
                **indicators.compute(name, self.df, digits=5, **params)}

    def news_for(self, symbol):
        self.calls.append(("news_for", symbol))
        return {"currencies": ["AUD", "NZD"], "blackout": {"active": False}}

    def news_search(self, query, hours=48):
        self.calls.append(("news_search", query, hours))
        return {"enabled": True, "articles": [{"titre": "RBA holds rates", "source": "x.com"}]}

    def major_events(self, hours=72):
        self.calls.append(("major_events", hours))
        return {"enabled": True, "agenda_fort_impact": [{"currency": "USD", "event": "CPI"}]}


# ----------------------------------------------------------------- indicateurs
def test_indicateurs_disponibles_et_calculables():
    df = _df()
    for name in ("ema", "rsi", "atr", "macd", "bbands", "stoch", "adx", "cci", "roc",
                 "donchian", "keltner", "supertrend", "vwap", "obv", "ichimoku"):
        out = indicators.compute(name, df, digits=5, period=14)
        assert "error" not in out, (name, out)
        assert out["value"] and out["read"]
        json.dumps(out)                      # doit rester serialisable pour le LLM


def test_indicateur_inconnu_et_donnees_insuffisantes():
    assert "disponibles" in indicators.compute("supercalifragilistic", _df())
    assert "error" in indicators.compute("rsi", _df(10))
    assert "error" in indicators.compute("rsi", pd.DataFrame())


def test_rsi_borne_et_adx_positif():
    r = indicators.compute("rsi", _df(), period=14)["value"]["rsi"]
    assert 0 <= r <= 100
    a = indicators.compute("adx", _df(), period=14)["value"]["adx"]
    assert a >= 0


# ----------------------------------------------------------------- chart libre
def test_chart_accepte_des_timeframes_arbitraires():
    frames = {"D1": _df(), "H4": _df(seed=3), "H1": _df(seed=5)}
    out = chart.read("AUDNZD", frames, {"digits": 5}, candles=5)
    assert out["timeframe_principal"] == "H4"          # 3 TF -> le 2e = structure
    assert set(out["context"]) == {"D1", "H4", "H1"}
    assert out["context"]["D1"]["role"] == "biais"
    assert out["context"]["H1"]["role"] == "timing"
    assert len(out["last_candles"]) == 5


def test_chart_deux_timeframes():
    out = chart.read("XAUUSD", {"D1": _df(), "H4": _df(seed=2)}, {"digits": 2})
    assert out["timeframe_principal"] == "D1"


# ----------------------------------------------------------------- outils LLM
def test_outils_libres_utilisent_le_provider_live():
    p = FakeProvider()
    T.bind_context({}, {}, {}, [], "", "", {})
    T.bind_live(p)

    univers = json.loads(T.list_symbols(query="AUD"))
    assert univers["symboles"][0]["symbol"] == "AUDNZD"

    # symbole hors watchlist -> charge a la demande
    snap = json.loads(T.get_market(symbol="AUDNZD", timeframe="H4"))
    assert snap["symbol"] == "AUDNZD"

    ch = json.loads(T.get_chart(symbol="AUDNZD", timeframes="D1,H4,H1", candles=6))
    assert ch["timeframe_principal"] == "H4" and len(ch["last_candles"]) == 6
    # sans timeframes -> profil par defaut du bot (l'orchestrateur tranche)
    assert json.loads(T.get_chart(symbol="AUDNZD"))["timeframe_principal"] == "D1"
    assert p.calls[-1][2] == ()

    ind = json.loads(T.compute_indicator(symbol="AUDNZD", indicator="adx",
                                         timeframe="H4", period=14))
    assert ind["indicator"] == "adx" and ind["timeframe"] == "H4"

    assert "RBA" in T.search_news(query="RBA decision", hours=24)
    assert "CPI" in T.get_macro_events(hours=48)
    assert "AUD" in T.get_news(symbol="AUDNZD")

    kinds = [c[0] for c in p.calls]
    assert kinds == ["symbols", "market", "chart", "chart", "indicator", "news_search",
                     "major_events", "news_for"]


def test_outils_degradent_sans_provider():
    T.bind_context({"EURUSD": {"symbol": "EURUSD"}}, {}, {}, [], "", "", {})
    T.bind_live(None)
    assert "error" in json.loads(T.list_symbols())
    assert json.loads(T.get_market(symbol="EURUSD"))["symbol"] == "EURUSD"   # cache du cycle
    assert "error" in json.loads(T.get_chart(symbol="AUDNZD"))
    assert "indisponible" in T.search_news(query="gold")


def test_news_construit_a_la_demande_un_symbole_hors_watchlist():
    from config import NewsConfig
    from data.news import NewsFeed

    class MemNews(NewsFeed):                 # cache en memoire (pas de state/ pollue)
        def __init__(self, cfg):
            super().__init__(cfg)
            self._mem, self.built = None, []

        def _read_cache(self):
            return self._mem

        def _write_cache(self, snap, ts=None):
            self._mem = dict(snap)

        def _build(self, symbols):
            self.built += list(symbols)
            return {"enabled": True, "fred": {},
                    "per_symbol": {s: {"currencies": ["X"]} for s in symbols},
                    "blackout": {s: {"active": s == "GBPJPY", "reason": "BoE",
                                     "in_hours": 0.4} for s in symbols}}

    feed = MemNews(NewsConfig(enabled=True))
    feed.snapshot(["EURUSD"])
    assert feed.built == ["EURUSD"]
    assert feed.blackout_for("GBPJPY")["active"] is True      # construit a la demande
    assert feed.built == ["EURUSD", "GBPJPY"]
    feed.for_symbols(["EURUSD"])                              # deja en cache -> pas de rebuild
    assert feed.built == ["EURUSD", "GBPJPY"]


def test_news_desactivees_pas_de_blackout():
    from config import NewsConfig
    from data.news import NewsFeed
    feed = NewsFeed(NewsConfig(enabled=False))
    assert feed.blackout_for("EURUSD")["active"] is False
    assert feed.major_events()["enabled"] is False
    assert feed.search("gold")["articles"] == []


# ----------------------------------------------------------------- enquete web
def _web(**kw):
    from config import WebConfig
    from data.web import WebResearch
    return WebResearch(WebConfig(**{"enabled": True, **kw}))


def test_web_refuse_les_urls_dangereuses():
    w = _web(deny_domains=("exemple-bloque.com",), allow_domains=())
    for url in ("ftp://fichiers.test/x", "file:///C:/secret.txt",
                "http://localhost:8000/admin", "http://127.0.0.1/x",
                "http://192.168.1.10/router", "http://exemple-bloque.com/page"):
        assert "refusee" in _web(deny_domains=("exemple-bloque.com",)).read(url)["error"], url
    assert w._check("http://10.0.0.1/x") is not None


def test_web_liste_blanche():
    w = _web(allow_domains=("federalreserve.gov",))
    assert "liste blanche" in w.read("https://autre-site.com/analyse")["error"]


def test_web_budget_par_cycle():
    calls = []

    class Stub(type(_web())):
        def _duckduckgo(self, q, limit):
            calls.append(q)
            return {"moteur": "duckduckgo", "query": q, "resultats": []}

    from config import WebConfig
    w = Stub(WebConfig(enabled=True, max_calls_per_cycle=1))
    assert "error" not in w.search("FOMC statement")
    assert "budget web epuise" in w.search("ECB press conference")["error"]
    assert w.search("FOMC statement")["query"] == "FOMC statement"    # cache : gratuit
    w.reset_budget()
    assert "error" not in w.search("BoJ policy")
    assert calls == ["FOMC statement", "BoJ policy"]


def test_web_desactive():
    w = _web(enabled=False)
    assert w.search("or")["enabled"] is False
    assert w.read("https://www.federalreserve.gov")["enabled"] is False


def test_extraction_texte_html():
    from data.web import _to_text
    html_page = ("<html><head><title>t</title><style>.a{color:red}</style></head><body>"
                 "<script>var x=1;</script><h1>Fed holds rates</h1>"
                 "<p>Inflation &amp; growth</p></body></html>")
    txt = _to_text(html_page)
    assert "Fed holds rates" in txt and "Inflation & growth" in txt
    assert "var x" not in txt and "color:red" not in txt


def test_sentiment_retail_parse_les_pourcentages():
    class Stub(type(_web())):
        def read(self, url, max_chars=None):
            return {"texte": "EURUSD 62% short 38% long\nXAUUSD 71% long 29% short\n"
                              "Bruit 300% 400%"}

    from config import WebConfig
    w = Stub(WebConfig(enabled=True))
    rows = w.retail_sentiment()["positionnement"]
    assert {r["symbole"] for r in rows} == {"EURUSD", "XAUUSD"}
    assert w.retail_sentiment("XAUUSD")["positionnement"][0]["part_1_pct"] == 71


def test_sentiment_retail_via_api_myfxbook():
    from config import WebConfig

    class Stub(type(_web())):
        def _myfxbook_api(self, symbol):
            return {"source": "myfxbook API (community outlook)",
                    "positionnement": [{"symbole": "EURUSD", "longs_pct": 38,
                                        "shorts_pct": 62}]}

        def read(self, url, max_chars=None):                 # ne doit pas etre appele
            raise AssertionError("l'API doit primer sur le scraping")

    w = Stub(WebConfig(enabled=True, myfxbook_email="a@b.c", myfxbook_password="x"))
    assert w.retail_sentiment("EURUSD")["positionnement"][0]["shorts_pct"] == 62


def test_sentiment_retail_explique_comment_activer_si_bloque():
    from config import WebConfig

    class Stub(type(_web())):
        def _myfxbook_api(self, symbol):
            return None                                       # pas d'identifiants
        def read(self, url, max_chars=None):
            return {"error": "page refusee par le site (HTTP 403)"}

    out = Stub(WebConfig(enabled=True)).retail_sentiment("EURUSD")
    assert "403" in out["error"] and "MYFXBOOK_EMAIL" in out["comment_activer"]


def test_outils_web_passent_par_le_provider():
    class P:
        def __init__(self):
            self.calls = []

        def web_search(self, query, limit=6):
            self.calls.append(("search", query, limit))
            return {"resultats": [{"titre": "FOMC minutes", "url": "https://fed.gov/x"}]}

        def web_read(self, url, max_chars=None):
            self.calls.append(("read", url, max_chars))
            return {"url": url, "texte": "Le FOMC maintient les taux"}

        def retail_sentiment(self, symbol=""):
            self.calls.append(("sentiment", symbol))
            return {"positionnement": [{"symbole": "EURUSD", "part_1_pct": 62}]}

        def fred_series(self, series_id, limit=12):
            self.calls.append(("fred", series_id, limit))
            return {"serie": series_id, "dernier": {"valeur": 3.1}}

    p = P()
    T.bind_context({}, {}, {}, [], "", "", {})
    T.bind_live(p)
    assert "FOMC minutes" in T.web_search(query="FOMC minutes", limit=3)
    assert "maintient" in T.web_read(url="https://fed.gov/x")
    assert "EURUSD" in T.get_retail_sentiment(symbol="EURUSD")
    assert "3.1" in T.get_fred_series(series_id="CPIAUCSL")
    assert [c[0] for c in p.calls] == ["search", "read", "sentiment", "fred"]

    T.bind_live(None)
    assert "indisponible" in T.web_search(query="x")
    assert "indisponible" in T.web_read(url="https://fed.gov")
    assert "indisponible" in T.get_fred_series(series_id="UNRATE")


def test_plan_trail_porte_le_timeframe_choisi():
    T.bind_context({}, {}, {}, [], "", "", {})
    T.plan_trail(ticket=11, atr_mult=2.5, activate_r=1.0, timeframe="h4")
    a = T.pop_actions()[0]
    assert a["type"] == "trail" and a["atr_mult"] == 2.5 and a["timeframe"] == "H4"
