PYTHON ?= python3
TWINE_ARGS ?=

.PHONY: clean clean-core clean-downloader \
	build-core-wheel build-core-sdist build-core check-core-dist publish-core \
	build-downloader-wheel build-downloader-sdist build-downloader check-downloader-dist publish-downloader

clean:
	rm -rf build dist ./*.egg-info
	$(MAKE) -C vesper_core clean PYTHON=$(PYTHON)
	$(MAKE) -C vesper_downloader clean PYTHON=$(PYTHON)

clean-core:
	$(MAKE) -C vesper_core clean PYTHON=$(PYTHON)

clean-downloader:
	$(MAKE) -C vesper_downloader clean PYTHON=$(PYTHON)

build-core-wheel:
	$(MAKE) -C vesper_core build-wheel PYTHON=$(PYTHON)

build-core-sdist:
	$(MAKE) -C vesper_core build-sdist PYTHON=$(PYTHON)

build-core:
	$(MAKE) -C vesper_core build PYTHON=$(PYTHON)

check-core-dist:
	$(MAKE) -C vesper_core check-dist PYTHON=$(PYTHON)

publish-core:
	$(MAKE) -C vesper_core publish PYTHON=$(PYTHON) TWINE_ARGS='$(TWINE_ARGS)'

build-downloader-wheel:
	$(MAKE) -C vesper_downloader build-wheel PYTHON=$(PYTHON)

build-downloader-sdist:
	$(MAKE) -C vesper_downloader build-sdist PYTHON=$(PYTHON)

build-downloader:
	$(MAKE) -C vesper_downloader build PYTHON=$(PYTHON)

check-downloader-dist:
	$(MAKE) -C vesper_downloader check-dist PYTHON=$(PYTHON)

publish-downloader:
	$(MAKE) -C vesper_downloader publish PYTHON=$(PYTHON) TWINE_ARGS='$(TWINE_ARGS)'
