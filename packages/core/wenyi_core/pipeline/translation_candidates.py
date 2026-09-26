"""Generate, checkpoint and judge independent candidates for one frozen batch."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from threading import RLock

from ..agents import prompts
from ..agents.polisher import Polisher
from ..agents.translator import Translator
from ..candidates import CandidateRecord, CandidateState, candidate_index_key, candidate_record_key
from ..config import Config
from ..i18n.prompts import render
from ..i18n.resources import prompt_fingerprint
from ..llm.base import LLMClient
from ..llm.choice import ChoiceRequest
from ..llm.limits import RequestStopped
from ..llm.routing import identity, inference_snapshot
from ..storage.protocol import Storage
from .translation_batch import BatchPlan, BatchResult, TranslationBatchExecutor


class TranslationCandidateService:
    """Own candidate concurrency; only the chapter service publishes formal targets."""

    def __init__(self, client: LLMClient, config: Config):
        self.client = client
        self.config = config

    def execute(
        self, plan: BatchPlan, store: Storage, *, flush_usage: Callable[[], object]
    ) -> tuple[BatchResult, CandidateRecord | None]:
        try:
            return self._execute(plan, store, flush_usage=flush_usage)
        finally:
            # Failed or interrupted requests still belong in the cumulative ledger.
            flush_usage()

    def _execute(
        self, plan: BatchPlan, store: Storage, *, flush_usage: Callable[[], object]
    ) -> tuple[BatchResult, CandidateRecord | None]:
        self.config.require_translation_judge()
        if not any(Translator._needs_translation(source) for source in plan.sources):
            return BatchResult(plan.sources, (None,) * len(plan.sources)), None
        manifest = store.load_manifest()
        source_hash = manifest.get("source_sha256", "")
        batch_id = identity(
            {
                "version": 1,
                "source_sha256": source_hash,
                "plan": asdict(plan),
                "source_lang": self.config.source_lang,
                "target_lang": self.config.target_lang,
                "honorific": self.config.honorific_strategy,
                "prompts": prompt_fingerprint(),
                "translation": inference_snapshot(self.config.llm, ["translation.body"]),
            }
        )
        key = candidate_record_key(plan.chapter, batch_id)
        saved = store.read_artifact(key)
        record = (
            CandidateRecord.model_validate(saved)
            if saved is not None
            else CandidateRecord(
                batch_id=batch_id,
                chapter=plan.chapter,
                segment_indices=list(plan.segment_indices),
                sources=list(plan.sources),
                status="generating",
                source_sha256=source_hash,
                source_lang=self.config.source_lang,
                target_lang=self.config.target_lang,
                candidates=[CandidateState(id=value) for value in ("A", "B", "C")],
            )
        )
        polish = self.config.pipeline.polish
        polish_fingerprint = identity(
            {
                "enabled": polish,
                "route": inference_snapshot(self.config.llm, ["polish.body"]) if polish else {},
            }
        )
        lock = RLock()

        def checkpoint() -> None:
            # Commit usage before making results reusable. The runtime serializes increments
            # through this batch lock and its existing recoverable ledger journal.
            flush_usage()
            store.write_artifact(key, record.model_dump(mode="json"))
            index_key = candidate_index_key(plan.chapter)
            index = store.read_artifact(index_key) or {}
            index.update({str(i): batch_id for i in plan.segment_indices})
            store.write_artifact(index_key, index)

        def event(name: str, **payload) -> None:
            store.log_event(name, chapter=plan.chapter, batch_id=batch_id, **payload)

        def run_candidate(candidate: CandidateState) -> None:
            translator = Translator(self.client, self.config)
            translator.alternative_instruction = candidate.alternative
            executor = TranslationBatchExecutor(translator, Polisher(self.client, self.config))
            if candidate.raw_targets is None:
                event(
                    "translation_candidate_started",
                    candidate=candidate.id,
                    generation=candidate.generation,
                )
                raw = executor.translate(plan)
                with lock:
                    candidate.raw_targets = raw
                    candidate.turn = translator.last_batch_turn
                    candidate.translated_indices = translator.last_batch_indices
                    checkpoint()
            if candidate.polish_fingerprint != polish_fingerprint or candidate.targets is None:
                raw = list(candidate.raw_targets)
                if polish:
                    with lock:
                        record.status = "polishing"
                        checkpoint()
                    event("translation_candidate_polishing", candidate=candidate.id)
                    result = executor.polish(
                        plan, raw, candidate.turn, candidate.translated_indices
                    )
                    targets = list(result.targets)
                else:
                    targets = raw
                with lock:
                    candidate.targets = targets
                    candidate.polish_status = "complete" if polish else "skipped"
                    candidate.polish_fingerprint = polish_fingerprint
                    record.decision = None
                    record.judge_fingerprint = None
                    checkpoint()
                event("translation_candidate_ready", candidate=candidate.id)

        checkpoint()
        errors: list[BaseException] = []
        with self.client.interrupt_scope(), ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(run_candidate, candidate) for candidate in record.candidates]
            for future in as_completed(futures):
                try:
                    future.result()
                except BaseException as error:
                    errors.append(error)
                    if isinstance(error, (RequestStopped, KeyboardInterrupt)):
                        self.client.cancel()
        if errors:
            stopped = next((error for error in errors if not isinstance(error, Exception)), None)
            raise stopped or errors[0]

        def duplicates() -> list[CandidateState]:
            first: dict[tuple[str, ...], str] = {}
            repeated = []
            for candidate in record.candidates:
                fingerprint = tuple(text.strip() for text in candidate.targets)
                candidate.duplicate_of = first.get(fingerprint)
                if candidate.duplicate_of:
                    repeated.append(candidate)
                else:
                    first[fingerprint] = candidate.id
            return repeated

        while (repeated := duplicates()) and record.extra_generations < 2:
            candidate = repeated[0]
            alternative = render(
                "translation_alternative",
                src=self.config.source_lang,
                tgt=self.config.target_lang,
                previous=prompts.numbered(candidate.targets),
            )
            record.extra_generations += 1
            replacement = CandidateState(
                id=candidate.id, alternative=alternative, generation=candidate.generation + 1
            )
            record.candidates[record.candidates.index(candidate)] = replacement
            record.decision = None
            record.judge_fingerprint = None
            record.status = "generating"
            checkpoint()  # Reserve the extra generation before making its first request.
            run_candidate(replacement)
        duplicates()
        judge_fingerprint = identity(
            {
                "candidates": [candidate.targets for candidate in record.candidates],
                "polish": polish_fingerprint,
                "routes": inference_snapshot(self.config.llm, ["translation.judge"]),
            }
        )
        if record.judge_fingerprint != judge_fingerprint or record.decision is None:
            record.decision = None
            record.judge_fingerprint = None
            record.status = "judging"
            checkpoint()
            event("translation_judge_started")
            request = ChoiceRequest(
                state={
                    "source_language": self.config.source_lang,
                    "target_language": self.config.target_lang,
                    "source": list(plan.sources),
                    "context": plan.context,
                    "next_source": plan.next_source,
                    "style": plan.style,
                    "glossary": prompts.render_glossary(list(plan.terms)),
                    "book_synopsis": plan.book_synopsis,
                    "chapter_digest": plan.chapter_digest,
                    "annotations": plan.annotation_contexts,
                },
                instructions=render(
                    "translation_judge", src=self.config.source_lang, tgt=self.config.target_lang
                ),
                options={candidate.id: candidate.targets for candidate in record.candidates},
            )
            record.decision = self.client.choose(request, operation="translation.judge")
            if record.decision.choice not in {candidate.id for candidate in record.candidates}:
                raise ValueError("The judge selected an unknown candidate")
            record.judge_fingerprint = judge_fingerprint
            record.status = "selected"
            checkpoint()
            event("translation_judge_selected", **asdict(record.decision))
        else:
            checkpoint()
        winner = next(c for c in record.candidates if c.id == record.decision.choice)
        return BatchResult(
            tuple(winner.targets),
            tuple(winner.raw_targets) if polish else (None,) * len(plan.sources),
        ), record

    @staticmethod
    def published(store: Storage, record: CandidateRecord) -> None:
        record.status = "published"
        store.write_artifact(
            candidate_record_key(record.chapter, record.batch_id), record.model_dump(mode="json")
        )
