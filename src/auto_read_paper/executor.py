from loguru import logger
from omegaconf import DictConfig
from .retriever import get_retriever_cls
from .reranker import get_reranker_cls
from .construct_email import render_email
from .utils import send_email
from .history import ScoreHistory, _today_iso
from .llm_client import LLMClient
from tqdm import tqdm


class Executor:
    def __init__(self, config: DictConfig):
        self.config = config
        self.retrievers = {
            source: get_retriever_cls(source)(config) for source in config.executor.source
        }
        self.reranker = get_reranker_cls(config.executor.reranker)(config)
        self.llm = LLMClient.from_config(config.llm)

        hist_cfg = config.get("history") if hasattr(config, "get") else None
        self.history: ScoreHistory | None = None
        if hist_cfg is not None and bool(hist_cfg.get("enabled", True)):
            self.history = ScoreHistory(
                path=str(hist_cfg.get("path", "state/score_history.json")),
                retention_days=int(hist_cfg.get("retention_days", 7)),
            )

    def run(self):
        today = _today_iso()

        # Surface effective arXiv filter config up-front so users can verify
        # CUSTOM_CONFIG actually took effect (vs silently falling back to the
        # committed config/custom.yaml).
        arxiv_cfg = self.config.get("source", {}).get("arxiv") if hasattr(self.config, "get") else None
        if arxiv_cfg is not None:
            cats = list(arxiv_cfg.get("category") or [])
            kws = list(arxiv_cfg.get("keywords") or [])
            logger.info(f"Effective arXiv categories: {cats}")
            logger.info(f"Effective arXiv keywords: {kws}")

        if self.history is not None:
            self.history.load()
            self.history.trim()

        all_papers = []
        for source, retriever in self.retrievers.items():
            logger.info(f"Retrieving {source} papers...")
            papers = retriever.retrieve_papers()
            if len(papers) == 0:
                logger.info(f"No {source} papers found")
                continue
            logger.info(f"Retrieved {len(papers)} {source} papers")
            all_papers.extend(papers)
        logger.info(f"Total {len(all_papers)} papers retrieved from all sources")

        # Skip papers we've already scored within the retention window.
        if self.history is not None:
            all_papers = self.history.filter_new_papers(all_papers)
            logger.info(f"{len(all_papers)} new papers need scoring today")

        # Score today's new papers.
        scored_today: list = []
        if all_papers:
            logger.info("Reranking papers (keyword filter + LLM scoring)...")
            scored_today = self.reranker.rerank(all_papers, [])

        # Record today's scores, then merge with unsent history into the candidate pool.
        # unsent_papers() never includes papers already sent on ANY day — each push
        # therefore only ever considers papers the user has not yet received. No
        # filler from sent_papers(), no content-hash dedup: multi-push-per-day is
        # a side-effect of the pool naturally shrinking as papers get marked sent.
        if self.history is not None:
            self.history.record_newly_scored(scored_today, today)
            pool = self.history.unsent_papers()
            logger.info(
                f"Candidate pool for today's email: {len(pool)} papers "
                f"(today={len(scored_today)} + unsent history)"
            )
        else:
            pool = list(scored_today)

        pool.sort(key=lambda p: p.score or 0.0, reverse=True)
        max_n = max(0, int(self.config.executor.max_paper_num))
        top_papers = pool[:max_n]

        # Last-resort heartbeat: if even the unsent pool is empty (first run,
        # very quiet day, or the user already consumed everything in previous
        # pushes today), pull a few recent arXiv papers so the pipeline still
        # produces a visible signal. These are scored and recorded as newly-
        # scored entries, NOT pulled from already-sent history.
        if not top_papers:
            arxiv_retriever = self.retrievers.get("arxiv")
            if arxiv_retriever is not None and hasattr(arxiv_retriever, "retrieve_fallback_papers"):
                logger.info("Pool empty — fetching recent arXiv papers as heartbeat fallback")
                try:
                    fb = arxiv_retriever.retrieve_fallback_papers(days=3, limit=max_n)
                except Exception as exc:
                    logger.warning(f"Heartbeat fallback failed: {exc}")
                    fb = []
                # Skip anything we've already sent so the heartbeat never
                # re-shows an old paper.
                if fb and self.history is not None:
                    sent_ids = {
                        e.get("id") for e in self.history.entries if e.get("sent_at")
                    }
                    from .history import _paper_id
                    fb = [p for p in fb if _paper_id(p) not in sent_ids]
                if fb:
                    logger.info(f"Scoring {len(fb)} heartbeat papers")
                    fb = self.reranker.rerank(fb, [])
                    fb.sort(key=lambda p: p.score or 0.0, reverse=True)
                    top_papers = fb[:max_n]
                    if self.history is not None:
                        self.history.record_newly_scored(top_papers, today)

        if not top_papers and not self.config.executor.send_empty:
            logger.info("No unsent papers available — no email will be sent.")
            if self.history is not None:
                self.history.save()
            return

        if top_papers:
            logger.info(f"Generating deep summaries for top {len(top_papers)} papers...")
            lang = str(self.config.llm.get("language", "Chinese"))
            for p in tqdm(top_papers):
                if not p.tldr:
                    p.generate_tldr(self.llm, lang)
                if not p.affiliations:
                    p.generate_affiliations(self.llm)
                if lang.lower() != "english" and not getattr(p, "title_zh", None):
                    p.generate_title_zh(self.llm, lang)

        lang = str(self.config.llm.get("language", "Chinese"))
        email_content = render_email(top_papers, lang)

        if self.history is not None:
            self.history.save()

        logger.info("Sending email...")
        send_email(self.config, email_content)
        logger.info("Email sent successfully")

        if self.history is not None:
            self.history.mark_sent(top_papers, today)
            self.history.save()
