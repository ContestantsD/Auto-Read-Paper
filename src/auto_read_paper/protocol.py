from dataclasses import dataclass
from typing import Optional, TypeVar
from datetime import datetime
import re
import tiktoken
from openai import OpenAI
from loguru import logger
import json
RawPaperItem = TypeVar('RawPaperItem')

_SECTION_ANCHORS = ("[CORE]", "[INNOVATION]", "[VALUE]")


def _clean_tldr(raw: str) -> str:
    """Extract the three-section TLDR from the LLM output.

    Reasoning-style models often leak chain-of-thought ("Let me write...", "Now I
    need to format...") or emit a draft plus a final restatement. We find the LAST
    occurrence of the [CORE] anchor — that's the clean final answer — then slice
    from there onward. Any preamble, meta-commentary, or duplicate earlier draft
    is discarded.
    """
    if not raw:
        return ""
    text = raw.strip().replace("\r\n", "\n")

    # Strip <think>...</think> style reasoning blocks if any model emits them.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)

    core_idx = text.rfind(_SECTION_ANCHORS[0])
    if core_idx == -1:
        # No structured output at all — strip obvious meta-commentary and return.
        text = re.sub(r"^(好的|好|当然|Sure|Okay|OK|Let me .*?|Now .*?|I need to .*?)[:：\n]",
                      "", text, flags=re.IGNORECASE | re.MULTILINE)
        return text.strip().replace("\n", "<br>")

    text = text[core_idx:]

    # Trim trailing noise at standalone markdown/section markers only.
    for marker in ("\n\n---", "\n\n##", "\n\n###"):
        cut = text.find(marker)
        if cut != -1:
            text = text[:cut]

    return text.strip().replace("\n", "<br>")


@dataclass
class Paper:
    source: str
    title: str
    authors: list[str]
    abstract: str
    url: str
    pdf_url: Optional[str] = None
    full_text: Optional[str] = None
    tldr: Optional[str] = None
    affiliations: Optional[list[str]] = None
    score: Optional[float] = None
    title_zh: Optional[str] = None

    def _generate_title_translation_with_llm(self, openai_client: OpenAI, llm_params: dict) -> Optional[str]:
        if not self.title:
            return None
        lang = str(llm_params.get('language', 'Chinese')).strip()
        if lang.lower() == 'english':
            return None
        response = openai_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You translate academic paper titles into {lang}. "
                        f"Produce a natural, professional, concise {lang} title. "
                        f"Keep widely-used English technical abbreviations (e.g. RL, MPC, LLM, RAG, BEV, GRPO) untranslated. "
                        f"Output ONLY the translated title on a single line — no quotes, no explanation, no extra content."
                    ),
                },
                {"role": "user", "content": f"Translate: {self.title}"},
            ],
            **llm_params.get('generation_kwargs', {}),
        )
        out = (response.choices[0].message.content or "").strip()
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL | re.IGNORECASE).strip()
        out = out.strip("\"'「」“”").splitlines()[-1].strip() if out else ""
        return out or None

    def generate_title_zh(self, openai_client: OpenAI, llm_params: dict) -> Optional[str]:
        try:
            title_zh = self._generate_title_translation_with_llm(openai_client, llm_params)
            self.title_zh = title_zh
            return title_zh
        except Exception as e:
            logger.warning(f"Failed to translate title of {self.url}: {e}")
            self.title_zh = None
            return None

    def _generate_tldr_with_llm(self, openai_client:OpenAI,llm_params:dict) -> str:
        lang = str(llm_params.get('language', 'Chinese')).strip() or 'Chinese'
        prompt = (
            f"Read the paper below and output a structured summary in {lang}, following the exact format.\n"
            f"Requirements:\n"
            f"1. Write the content in {lang}. Keep widely-used English technical abbreviations "
            f"(e.g. RL, MPC, RAG, LVLM, GRPO, LLM) in English; on first use, briefly gloss them in {lang} in parentheses.\n"
            f"2. Output ALL three sections below — none may be skipped. The anchor tags must appear exactly as written, verbatim.\n"
            f"3. Do not paraphrase the abstract literally, do not add any preamble, chain-of-thought, formatting notes, or closing remark. "
            f"Start the response directly with [CORE].\n\n"
            f"Use these three language-neutral anchor tags, in order:\n"
            f"[CORE] <1-2 sentences in {lang} describing the problem, the method, and the task setting>\n"
            f"[INNOVATION] <2-3 sentences in {lang}, more detailed: the pain point being solved, the core idea of the method, "
            f"and how it differs from / improves upon prior work>\n"
            f"[VALUE] <1-2 sentences in {lang} describing real-world impact, likely applications, or follow-up research value>\n\n"
        )
        if self.title:
            prompt += f"Title:\n {self.title}\n\n"

        if self.abstract:
            prompt += f"Abstract: {self.abstract}\n\n"

        if self.full_text:
            prompt += f"Preview of main content:\n {self.full_text}\n\n"

        if not self.full_text and not self.abstract:
            logger.warning(f"Neither full text nor abstract is provided for {self.url}")
            return "Failed to generate TLDR. Neither full text nor abstract is provided"

        # use gpt-4o tokenizer for estimation
        enc = tiktoken.encoding_for_model("gpt-4o")
        prompt_tokens = enc.encode(prompt)
        prompt_tokens = prompt_tokens[:4000]  # truncate to 4000 tokens
        prompt = enc.decode(prompt_tokens)

        response = openai_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You are a senior AI researcher summarising academic papers for busy readers. "
                        f"Write the entire response in {lang}. Only widely-used English technical abbreviations "
                        f"(e.g. RL, MPC, RAG, LLM) may stay in English — gloss them once in {lang} on first mention. "
                        f"You MUST emit exactly three sections in this order, using the anchor tags [CORE], [INNOVATION], [VALUE] verbatim "
                        f"(do not translate the anchor tags). Every section is mandatory — none may be skipped. "
                        f"[INNOVATION] must be 2-3 sentences and more detailed: the pain point it solves, the core idea, "
                        f"and how it differs from or improves upon prior work. [CORE] and [VALUE] are each 1-2 sentences. "
                        f"Do NOT output any chain-of-thought, preamble, plan, or closing note. "
                        f"Do NOT quote the abstract verbatim. Start your answer directly with [CORE]."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            **llm_params.get('generation_kwargs', {})
        )
        tldr = response.choices[0].message.content or ""
        tldr = _clean_tldr(tldr)
        return tldr
    
    def generate_tldr(self, openai_client:OpenAI,llm_params:dict) -> str:
        try:
            tldr = self._generate_tldr_with_llm(openai_client,llm_params)
            self.tldr = tldr
            return tldr
        except Exception as e:
            logger.warning(f"Failed to generate tldr of {self.url}: {e}")
            tldr = self.abstract
            self.tldr = tldr
            return tldr

    def _generate_affiliations_with_llm(self, openai_client:OpenAI,llm_params:dict) -> Optional[list[str]]:
        if self.full_text is not None:
            prompt = f"Given the beginning of a paper, extract the affiliations of the authors in a python list format, which is sorted by the author order. If there is no affiliation found, return an empty list '[]':\n\n{self.full_text}"
            # use gpt-4o tokenizer for estimation
            enc = tiktoken.encoding_for_model("gpt-4o")
            prompt_tokens = enc.encode(prompt)
            prompt_tokens = prompt_tokens[:2000]  # truncate to 2000 tokens
            prompt = enc.decode(prompt_tokens)
            affiliations = openai_client.chat.completions.create(
                messages=[
                    {
                        "role": "system",
                        "content": "You are an assistant who perfectly extracts affiliations of authors from a paper. You should return a python list of affiliations sorted by the author order, like [\"TsingHua University\",\"Peking University\"]. If an affiliation is consisted of multi-level affiliations, like 'Department of Computer Science, TsingHua University', you should return the top-level affiliation 'TsingHua University' only. Do not contain duplicated affiliations. If there is no affiliation found, you should return an empty list [ ]. You should only return the final list of affiliations, and do not return any intermediate results.",
                    },
                    {"role": "user", "content": prompt},
                ],
                **llm_params.get('generation_kwargs', {})
            )
            affiliations = affiliations.choices[0].message.content

            affiliations = re.search(r'\[.*?\]', affiliations, flags=re.DOTALL).group(0)
            affiliations = json.loads(affiliations)
            affiliations = list(set(affiliations))
            affiliations = [str(a) for a in affiliations]

            return affiliations
    
    def generate_affiliations(self, openai_client:OpenAI,llm_params:dict) -> Optional[list[str]]:
        try:
            affiliations = self._generate_affiliations_with_llm(openai_client,llm_params)
            self.affiliations = affiliations
            return affiliations
        except Exception as e:
            logger.warning(f"Failed to generate affiliations of {self.url}: {e}")
            self.affiliations = None
            return None
@dataclass
class CorpusPaper:
    title: str
    abstract: str
    added_date: datetime
    paths: list[str]