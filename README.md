# Buscador de PDFs

Ferramenta desktop (Windows) que indexa todos os PDFs de uma pasta (e subpastas)
e permite buscar uma palavra/trecho, mostrando em quais arquivos e páginas ela
aparece. Documentos idênticos (mesmo conteúdo) são agrupados como um só. PDFs
que são só imagem (escaneados) são lidos via OCR (Tesseract), quando disponível.

## Download

Baixe o executável pronto (não precisa instalar Python) na
[página do projeto](https://matheusmerlim1.github.io/buscador-de-pdfs/) ou
direto pela [última release](https://github.com/matheusmerlim1/buscador-de-pdfs/releases/latest).

## Funcionalidades

- Indexação recursiva de uma pasta inteira, com cache em SQLite (só reprocessa
  PDFs novos ou alterados).
- Busca por palavra ou trecho, com o número de ocorrências e um trecho de
  contexto por página.
- Agrupamento automático de PDFs idênticos (mesmo hash de conteúdo).
- OCR automático (Tesseract) para páginas que são apenas imagem.
- Abre o PDF direto na página encontrada, com um duplo-clique, nos leitores
  mais comuns (Foxit, Adobe Acrobat/Reader, PDF-XChange, SumatraPDF, Edge).

## Estrutura

```text
.
|-- .github/workflows/
|   |-- secret-scan.yml
|   `-- ci.yml
|-- src/
|   `-- pdf_search_app.py
|-- tests/
|   `-- test_pdf_search_app.py
|-- conftest.py               # poe src/ no sys.path para os testes
|-- index.html                # pagina de download (GitHub Pages)
|-- BuscadorDePDFs.spec       # build PyInstaller (empacota src/pdf_search_app.py)
|-- requirements.txt          # dependencias de runtime
|-- requirements-dev.txt      # + pytest, para rodar os testes
|-- requirements-build.txt    # + pyinstaller, para gerar o .exe
`-- .gitignore
```

`config.json`, `indexes/` e `BuscadorDePDFs.exe` são estado de execução e
artefato de build, respectivamente — ficam fora do controle de versão
(`.gitignore`) e são gerados/atualizados localmente.

## Desenvolvimento

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
python src\pdf_search_app.py
```

## Build do executável (Windows)

```powershell
python -m pip install -r requirements-build.txt
pyinstaller BuscadorDePDFs.spec
```

O executável gerado fica em `dist\BuscadorDePDFs.exe`.
