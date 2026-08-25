.PHONY: help install sample demo api worker test bench clean

help:
	@echo "make install  - install dependencies"
	@echo "make sample   - generate a synthetic slide in data/"
	@echo "make demo     - sample + submit + process + print the report"
	@echo "make api      - run the API on :8000"
	@echo "make worker   - run the worker"
	@echo "make test     - run the test suite"
	@echo "make bench    - throughput and memory benchmark"

install:
	pip install -r requirements.txt

sample:
	python scripts/make_sample_slide.py data/sample.tif --width 4096 --height 3072

demo: sample
	python -m wsiqc submit data/sample.tif --tile-size 256 --downsample 2.0
	python -m wsiqc worker --once
	python -m wsiqc jobs

api:
	uvicorn wsiqc.api.main:app --reload --port 8000

worker:
	python -m wsiqc worker

test:
	pytest -q

bench:
	python scripts/benchmark.py data/sample.tif

clean:
	rm -rf out *.db *.db-wal *.db-shm .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
