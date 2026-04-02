from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.core.config import settings


def configure_tracing() -> trace.Tracer:
    resource = Resource.create(
        {
            "service.name": "yang-ban",
            "deployment.environment": settings.app_env,
        }
    )

    provider = TracerProvider(resource=resource)

    if settings.langfuse_public_key and settings.langfuse_secret_key:
        exporter = OTLPSpanExporter(
            endpoint=f"{settings.langfuse_host}/api/public/otel/v1/traces",
            headers={
                "Authorization": f"Basic {settings.langfuse_public_key}:{settings.langfuse_secret_key}",
            },
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)

    return trace.get_tracer("yang-ban")


tracer: trace.Tracer = configure_tracing()
