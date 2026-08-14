import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from r1999extractor.bulk_generation import (
    CommandProvider,
    load_generation_queue,
    run_bulk_generation,
)
from r1999extractor.cli import cli_error
from r1999extractor.generation_queue import default_output as default_queue
from r1999extractor.settings import get_local_data_directory
from r1999extractor.versioned_json import VersionedJSONCodec

benchmark_schema = "r1999.voice-model-benchmark"
benchmark_schema_version = 1
benchmark_codec = VersionedJSONCodec(
    benchmark_schema, benchmark_schema_version, "voice model benchmark"
)
default_output = get_local_data_directory() / "reverse1999" / "model-benchmark"


class ModelBenchmarkError(RuntimeError):
    pass


def select_representative_items(items, sample_size=24):
    eligible = [item for item in items if item.get("action") == "generate"]
    buckets = defaultdict(list)
    for item in eligible:
        emotion = item.get("emotion", {}).get("primary", "neutral")
        buckets[emotion].append(item)
    selected = []
    while len(selected) < sample_size and buckets:
        empty = []
        for emotion in sorted(buckets):
            if len(selected) >= sample_size:
                break
            if buckets[emotion]:
                selected.append(buckets[emotion].pop(0))
            if not buckets[emotion]:
                empty.append(emotion)
        for emotion in empty:
            buckets.pop(emotion, None)
    return selected


def benchmark_models(queue_path, output_directory, providers, *, sample_size=24, seed=0):
    queue_metadata, items = load_generation_queue(queue_path)
    sample = select_representative_items(items, sample_size)
    if not sample:
        raise ModelBenchmarkError("Generation queue has no generation-ready samples")
    output_directory = Path(output_directory).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    sample_queue = output_directory / "benchmark-queue.jsonl"
    metadata = dict(queue_metadata)
    metadata["item_count"] = len(sample)
    with sample_queue.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n")
        for item in sample:
            stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    models = []
    for provider in providers:
        model_root = output_directory / f"{provider.provider}-{provider.model}"
        result = run_bulk_generation(
            sample_queue,
            model_root,
            provider,
            limit=len(sample),
            retries=1,
            seed=seed,
        )
        state = json.loads(result["state"].read_text(encoding="utf-8"))
        qualities = [
            item["quality"]
            for item in state["items"].values()
            if item.get("status") in {"generated", "approved"} and "quality" in item
        ]
        models.append(
            {
                "provider": provider.provider,
                "model": provider.model,
                "generated_count": len(qualities),
                "failed_count": result["failed"],
                "technical_success_rate": round(len(qualities) / len(sample), 4),
                "average_peak": round(
                    sum(item["peak"] for item in qualities) / len(qualities), 6
                )
                if qualities
                else None,
                "average_duration_seconds": round(
                    sum(item["duration_seconds"] for item in qualities) / len(qualities), 4
                )
                if qualities
                else None,
                "manifest": str(result["manifest"]),
                "manual_scores": {
                    "emotion": None,
                    "voice_consistency": None,
                    "naturalness": None,
                    "pronunciation": None,
                },
            }
        )
    report = benchmark_codec.new(
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_queue=str(Path(queue_path).expanduser().resolve()),
        sample_count=len(sample),
        sample_queue=str(sample_queue),
        manual_review_required=True,
        models=models,
    )
    benchmark_codec.write(output_directory / "benchmark-report.json", report, sort_keys=True)
    return report


def load_provider_config(path):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelBenchmarkError(f"Unable to read model config {path}: {error}") from error
    if not isinstance(document, list) or not document:
        raise ModelBenchmarkError("Model config must be a non-empty list")
    providers = []
    for index, item in enumerate(document):
        if not isinstance(item, dict) or not all(
            isinstance(item.get(field), str) and item[field].strip()
            for field in ("provider", "model", "command")
        ):
            raise ModelBenchmarkError(f"Model config entry {index} is invalid")
        providers.append(
            CommandProvider(item["command"], provider=item["provider"], model=item["model"])
        )
    return providers


def create_parser():
    parser = argparse.ArgumentParser(
        description="Compare local voice models on a shared emotion-stratified sample."
    )
    parser.add_argument("--queue", type=Path, default=default_queue)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--sample-size", type=int, default=24)
    parser.add_argument("--seed", type=int, default=0)
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
        providers = load_provider_config(options.models)
        report = benchmark_models(
            options.queue,
            options.output,
            providers,
            sample_size=options.sample_size,
            seed=options.seed,
        )
    except (ModelBenchmarkError, OSError) as error:
        return cli_error(error)
    print(
        f"Benchmarked {len(report['models'])} model(s) on {report['sample_count']} shared lines; "
        f"complete manual scores in {options.output / 'benchmark-report.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
