from dataclasses import dataclass, asdict, field
from typing import List, Optional


@dataclass
class StreamCandidate:
    url: str
    title: str = ""
    source: str = ""
    confidence: int = 0
    reason: str = ""
    originUrl: str = ""
    originType: str = ""

    originalUrl: str = ""
    stableUrl: str = ""
    originAction: str = ""

    qualityHint: str = ""
    qualityScore: int = 0

    isTemporary: bool = False
    requiresFreshDiscovery: bool = False

    isPlayable: bool = False
    httpStatusCode: int = 0
    contentType: str = ""
    finalUrl: str = ""


@dataclass
class ParserResult:
    success: bool
    inputUrl: str
    effectiveUrl: str = ""
    title: str = ""
    candidates: List[StreamCandidate] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self):
        return asdict(self)
