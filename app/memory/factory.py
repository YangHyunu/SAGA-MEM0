from app.core.config import Settings
from app.memory.base import MemoryBackend
from app.memory.mem0_backend import InstrumentedMemory


def create_memory_backend(cfg: Settings) -> MemoryBackend:
    if cfg.memory_backend == "mem0":
        return InstrumentedMemory(cfg)
    raise ValueError("unknown_memory_backend: " + cfg.memory_backend)
