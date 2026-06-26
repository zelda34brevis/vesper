PYTHON ?= python3
TWINE_ARGS ?=
TOOLS_VENV ?= .release-venv
TOOLS_PYTHON := $(TOOLS_VENV)/bin/python
TOOLS_STAMP := $(TOOLS_VENV)/.ready

.PHONY: clean bootstrap-tools build-wheel build-sdist build check-dist publish

clean:
	rm -rf build dist ./*.egg-info

$(TOOLS_STAMP):
	rm -rf $(TOOLS_VENV)
	$(PYTHON) -m venv $(TOOLS_VENV)
	$(TOOLS_PYTHON) -m pip install --quiet --upgrade pip build twine
	touch $(TOOLS_STAMP)

bootstrap-tools: $(TOOLS_STAMP)

build-wheel: $(TOOLS_STAMP)
	$(TOOLS_PYTHON) -P -m build --wheel

build-sdist: $(TOOLS_STAMP)
	$(TOOLS_PYTHON) -P -m build --sdist

build: clean $(TOOLS_STAMP)
	$(TOOLS_PYTHON) -P -m build

check-dist: $(TOOLS_STAMP)
	$(TOOLS_PYTHON) -P -m twine check dist/*

publish: $(TOOLS_STAMP)
	$(TOOLS_PYTHON) -P -m twine upload $(TWINE_ARGS) dist/*
