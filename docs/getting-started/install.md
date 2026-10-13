# Install

What you need on Windows, macOS or Linux to build documents and run the editor, either from Python or from the single-file binaries.

## Requirements

- Python 3.9 or newer. The scripts use only the standard library.
- Python 3.9 and 3.10 also need the `tomli` package to read `build.toml` (see [build.toml keys](../reference/build-toml.md)). Python 3.11 and newer do not.
- A LaTeX distribution that includes `latexmk`.

| System | LaTeX distribution |
| --- | --- |
| Windows | [MiKTeX](https://miktex.org/) or [TeX Live](https://www.tug.org/texlive/) |
| macOS | [MacTeX](https://www.tug.org/mactex/) |
| Debian/Ubuntu | see the command below |

```bash
sudo apt install latexmk texlive-latex-extra texlive-plain-generic texlive-fonts-recommended texlive-science texlive-xetex texlive-luatex texlive-extra-utils texlive-bibtex-extra biber chktex latexdiff
```

Optional tools:

| Tool | Needed for |
| --- | --- |
| [uv](https://docs.astral.sh/uv/) | running the scripts with `uv run` |
| [pandoc](https://pandoc.org/installing.html) | DOCX export |
| `dvisvgm` and `poppler-utils` | figures in the HTML export |

## Check the installation

```bash
latexmk --version
```

If the command is not found, add your LaTeX installation to `PATH` and restart the terminal.

## Run with uv

With uv you can use `uv run` instead of `python`:

```bash
uv run scripts/build.py
```

## Use the binaries (no Python)

Each version release (`v*` tags, not the `pdfs` release) carries single-file builds of `build.py` and `serve.py`. They take the same flags as the scripts. LaTeX is still required.

| Platform | Files |
| --- | --- |
| Linux | `build-linux-x64`, `serve-linux-x64` |
| macOS (Apple silicon) | `build-macos-arm64`, `serve-macos-arm64` |
| Windows | `build-windows-x64.exe`, `serve-windows-x64.exe` |

1. Download the binary for your platform from the version release.
2. On Linux and macOS, make it executable: `chmod +x build-linux-x64`.
3. Run it from the project folder, the one holding `files/`. A binary treats the directory it starts in as the repository, so `out/` and `.latex-cache/` land there.

The binaries are not signed, so macOS and Windows may warn on the first start. The editor binary loads its libraries from the CDN; the offline editor (`scripts/vendor_ui.py`) needs the Python version.

## Next

- [Just compiling LaTeX](compile-only.md)
- [Building documents](../guides/building.md)
- [Command-line reference](../reference/cli.md)
