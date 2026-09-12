"""
Buscador de PDFs - indexa todos os PDFs de uma pasta (e subpastas) e permite
buscar uma palavra/trecho, mostrando em quais arquivos e paginas ela aparece.
Documentos identicos (mesmo conteudo) sao agrupados como um so.
PDFs que sao so imagem (escaneados) sao lidos via OCR (Tesseract), quando disponivel.
"""
import os
import sys
import json
import re
import time
import shutil
import shlex
import hashlib
import sqlite3
import subprocess
import threading
import unicodedata
from urllib.parse import quote

try:
    import winreg  # so existe no Windows; o CI e os testes rodam em Linux
except ImportError:
    winreg = None

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

import fitz  # PyMuPDF

try:
    import sv_ttk  # tema visual estilo Windows 11
except Exception:
    sv_ttk = None

try:
    import pytesseract
    from PIL import Image
except Exception:
    pytesseract = None
    Image = None

APP_TITLE = "Buscador de PDFs"
INDEX_FILENAME = ".pdf_search_index.sqlite3"
CONFIG_FILENAME = "config.json"
OCR_MIN_TEXT_LEN = 20  # abaixo disso, a pagina e tratada como "so imagem" e vai para OCR
OCR_DPI = 200


def base_dir():
    """Pasta onde o executavel/script esta rodando."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def local_index_dir():
    """Pasta local (ao lado do programa) usada como alternativa quando a
    pasta dos PDFs e somente-leitura (ex.: unidade de rede corporativa)."""
    d = os.path.join(base_dir(), "indexes")
    os.makedirs(d, exist_ok=True)
    return d


def hashed_index_name(folder):
    h = hashlib.sha1(os.path.normcase(os.path.abspath(folder)).encode("utf-8")).hexdigest()[:16]
    return f".pdf_search_index_{h}.sqlite3"


def config_path():
    return os.path.join(base_dir(), CONFIG_FILENAME)


def load_last_folder():
    """Le a ultima pasta usada (config.json). Se nao houver, usa a pasta-pai
    (a ferramenta normalmente fica numa subpasta dentro da pasta com os PDFs)."""
    cfg = config_path()
    if os.path.exists(cfg):
        try:
            with open(cfg, "r", encoding="utf-8") as f:
                data = json.load(f)
            folder = data.get("folder")
            if folder and os.path.isdir(folder):
                return folder
        except Exception:
            pass
    parent = os.path.dirname(base_dir())
    return parent if parent else base_dir()


def save_last_folder(folder):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump({"folder": folder}, f)
    except Exception:
        pass


def normalize(text):
    """Remove acentos, baixa para minusculas e troca quebras de linha por espaco
    (para uma busca por frase nao falhar so porque o PDF/OCR quebrou a linha no
    meio dela). Troca 1-para-1 (\\r e \\n viram 1 espaco cada) para preservar os
    indices de caracteres em relacao ao texto original."""
    if not text:
        return ""
    text = text.replace("\r", " ").replace("\n", " ")
    nfkd = unicodedata.normalize("NFKD", text)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return sem_acento.lower()


def file_hash(path):
    """Hash do conteudo do arquivo, usado para detectar PDFs duplicados (mesmo documento)."""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def find_tesseract():
    """Procura o executavel do Tesseract em locais comuns do Windows, alem do PATH."""
    candidates = [
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def setup_ocr():
    """Configura o pytesseract e devolve (disponivel, idiomas)."""
    if pytesseract is None:
        return False, ""
    exe = find_tesseract()
    if not exe:
        return False, ""
    pytesseract.pytesseract.tesseract_cmd = exe
    try:
        langs = set(pytesseract.get_languages(config=""))
    except Exception:
        langs = set()
    lang = "+".join(l for l in ("por", "eng") if l in langs) or ("eng" if "eng" in langs else None)
    if not lang:
        return False, ""
    return True, lang


OCR_AVAILABLE, OCR_LANG = setup_ocr()


def ocr_page(page):
    """Renderiza a pagina como imagem e roda OCR. Retorna texto (pode ser vazio)."""
    if not OCR_AVAILABLE:
        return ""
    try:
        zoom = OCR_DPI / 72
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return pytesseract.image_to_string(img, lang=OCR_LANG) or ""
    except Exception:
        return ""


def default_pdf_reader_exe():
    """Descobre o executavel associado a .pdf no Windows (o programa que abre
    ao dar duplo-clique num PDF), lendo o registro do sistema."""
    if winreg is None:
        return None
    progid = None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice",
        ) as k:
            progid = winreg.QueryValueEx(k, "ProgId")[0]
    except OSError:
        pass
    if not progid:
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, ".pdf") as k:
                progid = winreg.QueryValueEx(k, "")[0] or None
        except OSError:
            return None
    if not progid:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{progid}\\shell\\open\\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
    except OSError:
        return None
    try:
        parts = shlex.split(cmd, posix=False)
    except ValueError:
        parts = cmd.split()
    if not parts:
        return None
    exe = parts[0].strip('"')
    return exe if os.path.exists(exe) else None


def open_pdf_at_page(path, page):
    """Abre o PDF no leitor padrao do Windows, tentando pular direto para a
    pagina indicada. Cada leitor tem sua propria sintaxe de linha de comando;
    se o leitor nao for reconhecido ou a tentativa falhar, abre o arquivo
    normalmente (sem pular pagina). Retorna True se conseguiu abrir de algum jeito."""
    exe = default_pdf_reader_exe()
    if exe and page:
        name = os.path.basename(exe).lower()
        try:
            if "sumatra" in name:
                subprocess.Popen([exe, "-page", str(page), path])
                return True
            if "msedge" in name or "chrome" in name:
                uri = "file:///" + quote(path.replace("\\", "/"), safe="/:") + f"#page={page}"
                subprocess.Popen([exe, uri])
                return True
            # Foxit, Adobe Acrobat/Reader, PDF-XChange e a maioria dos outros
            # leitores populares seguem a mesma convencao: /A "page=N"
            subprocess.Popen([exe, "/A", f"page={page}", path])
            return True
        except Exception:
            pass
    try:
        os.startfile(path)
        return True
    except Exception:
        return False


def escape_like(term):
    """Escapa % _ e \\ para uso seguro dentro de um LIKE ... ESCAPE '\\'."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PDFIndex:
    """Indice persistido em SQLite: grava o resultado de cada PDF assim que ele
    e analisado (nao so no final), para nao perder progresso em pastas grandes
    (milhares de PDFs) e para nao precisar carregar todo o texto na memoria."""

    def __init__(self, folder):
        self.folder = folder
        self.used_fallback_dir = False
        preferred = os.path.join(folder, INDEX_FILENAME)
        try:
            self.conn = sqlite3.connect(preferred, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.db_path = preferred
        except sqlite3.OperationalError:
            # pasta somente-leitura (ex.: unidade de rede corporativa) -
            # guarda o indice localmente, ao lado do programa
            fallback = os.path.join(local_index_dir(), hashed_index_name(folder))
            self.conn = sqlite3.connect(fallback, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.db_path = fallback
            self.used_fallback_dir = True
        # duas conexoes (WAL permite 1 escritor + leitores simultaneos sem
        # bloquear buscas enquanto uma indexacao grande esta rodando)
        self.read_conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                rel_path TEXT PRIMARY KEY,
                mtime REAL,
                hash TEXT,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS pages (
                rel_path TEXT,
                page_num INTEGER,
                text TEXT,
                norm_text TEXT,
                ocr INTEGER,
                PRIMARY KEY (rel_path, page_num)
            );
            CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(hash);
            """
        )
        self.conn.commit()

    def close(self):
        for c in (self.conn, self.read_conn):
            try:
                c.close()
            except Exception:
                pass

    def count(self):
        return self.read_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def find_pdfs(self):
        pdfs = []
        for root, _dirs, files in os.walk(self.folder):
            for name in files:
                if name.lower().endswith(".pdf"):
                    full = os.path.join(root, name)
                    pdfs.append(full)
        return pdfs

    def _get_cached_mtime(self, rel):
        row = self.conn.execute("SELECT mtime FROM documents WHERE rel_path=?", (rel,)).fetchone()
        return row[0] if row else None

    def _store_document(self, rel, mtime, hash_, pages, error=None):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM pages WHERE rel_path=?", (rel,))
        cur.execute(
            "INSERT INTO documents (rel_path, mtime, hash, error) VALUES (?,?,?,?) "
            "ON CONFLICT(rel_path) DO UPDATE SET mtime=excluded.mtime, hash=excluded.hash, error=excluded.error",
            (rel, mtime, hash_, error),
        )
        cur.executemany(
            "INSERT INTO pages (rel_path, page_num, text, norm_text, ocr) VALUES (?,?,?,?,?)",
            [(rel, pnum, text, normalize(text), 1 if ocr else 0) for pnum, text, ocr in pages],
        )
        self.conn.commit()

    def rebuild(self, progress_cb=None, stop_flag=None, force=False):
        """Reindexa arquivos novos ou modificados (ou todos, se force=True).
        Cada PDF e salvo no banco assim que termina de ser lido. Remove do
        indice os que sumiram da pasta."""
        pdfs = self.find_pdfs()
        seen = set()
        total = len(pdfs)

        for i, full in enumerate(pdfs):
            if stop_flag and stop_flag.is_set():
                break
            rel = os.path.relpath(full, self.folder)
            seen.add(rel)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue

            cached_mtime = self._get_cached_mtime(rel)
            if not force and cached_mtime is not None and abs(cached_mtime - mtime) < 1:
                if progress_cb:
                    progress_cb(i + 1, total, rel, cached=True, ocr=False)
                continue

            pages = []  # (pnum, text, ocr_bool)
            try:
                doc = fitz.open(full)
                for pnum, page in enumerate(doc, start=1):
                    text = page.get_text("text") or ""
                    used_ocr = False
                    if len(text.strip()) < OCR_MIN_TEXT_LEN and OCR_AVAILABLE:
                        if stop_flag and stop_flag.is_set():
                            break
                        ocr_text = ocr_page(page)
                        if ocr_text.strip():
                            text = (text + "\n" + ocr_text).strip()
                            used_ocr = True
                            if progress_cb:
                                progress_cb(i + 1, total, f"{rel} (OCR pag {pnum})", cached=False, ocr=True)
                    pages.append((pnum, text, used_ocr))
                doc.close()
                self._store_document(rel, mtime, file_hash(full), pages)
            except Exception as e:
                self._store_document(rel, mtime, None, [], error=str(e))

            if progress_cb:
                progress_cb(i + 1, total, rel, cached=False, ocr=False)

        # remove entradas de arquivos que nao existem mais na pasta
        existing = {r[0] for r in self.conn.execute("SELECT rel_path FROM documents").fetchall()}
        gone = existing - seen
        if gone:
            cur = self.conn.cursor()
            cur.executemany("DELETE FROM documents WHERE rel_path=?", [(r,) for r in gone])
            cur.executemany("DELETE FROM pages WHERE rel_path=?", [(r,) for r in gone])
            self.conn.commit()

    def forget(self, rel):
        """Remove um documento do indice (para forcar reanalise)."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM documents WHERE rel_path=?", (rel,))
        cur.execute("DELETE FROM pages WHERE rel_path=?", (rel,))
        self.conn.commit()

    def _doc_groups_for_hashes(self, hashes):
        """Para um conjunto de hashes, devolve {hash: [rel_paths ordenados]}."""
        if not hashes:
            return {}
        placeholders = ",".join("?" * len(hashes))
        rows = self.read_conn.execute(
            f"SELECT rel_path, hash FROM documents WHERE hash IN ({placeholders})", list(hashes)
        ).fetchall()
        by_hash = {}
        for rel, h in rows:
            by_hash.setdefault(h, []).append(rel)
        for h in by_hash:
            by_hash[h].sort(key=lambda r: (len(r), r))
        return by_hash

    def search(self, query, limit_pages=5000):
        """Retorna lista de dicts, um por documento unico (copias identicas agrupadas):
        {rel, all_paths, pages: [(pagina, ocorrencias, trecho)], total}"""
        q_norm = normalize(query.strip())
        if not q_norm:
            return []
        pattern = "%" + escape_like(q_norm) + "%"
        rows = self.read_conn.execute(
            "SELECT p.rel_path, p.page_num, p.text, d.hash "
            "FROM pages p JOIN documents d ON d.rel_path = p.rel_path "
            "WHERE p.norm_text LIKE ? ESCAPE '\\' LIMIT ?",
            (pattern, limit_pages),
        ).fetchall()

        # agrupa por hash de conteudo (documentos identicos viram 1 so)
        hashes = {h for *_, h in rows if h}
        groups = self._doc_groups_for_hashes(hashes)

        per_doc = {}  # chave de agrupamento -> {"rels": set/list, "pages": {pnum: (count, snippet)}}
        for rel, pnum, text, h in rows:
            norm_text = normalize(text)
            count = norm_text.count(q_norm)
            if count == 0:
                continue
            idx = norm_text.find(q_norm)
            start = max(0, idx - 60)
            end = min(len(text), idx + len(q_norm) + 60)
            snippet = re.sub(r"\s+", " ", text[start:end].replace("\n", " ").strip())

            key = h if h else rel
            entry = per_doc.setdefault(key, {"rels": groups.get(h, [rel]) if h else [rel], "pages": {}})
            entry["pages"][pnum] = (count, snippet)

        results = []
        for key, entry in per_doc.items():
            pages_sorted = sorted(entry["pages"].items())
            total = sum(c for _, (c, _s) in pages_sorted)
            main = entry["rels"][0]
            results.append({
                "rel": main,
                "all_paths": entry["rels"],
                "pages": [(p, c, s) for p, (c, s) in pages_sorted],
                "total": total,
            })

        results.sort(key=lambda r: (-r["total"], r["rel"]))
        return results


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1040x620")
        self.minsize(760, 420)

        if sv_ttk is not None:
            try:
                sv_ttk.set_theme("light")
            except Exception:
                pass
        self._apply_win11_style()

        self.folder = load_last_folder()
        self.index = PDFIndex(self.folder)
        self.stop_flag = threading.Event()
        self.indexing = False

        self._build_ui()
        self._refresh_status_only()
        self.status_var.set(
            'Confira a pasta acima (ou escolha/edite outra) e clique em "Atualizar" para iniciar a analise dos PDFs.'
        )

    # ---------------- visual ----------------
    def _apply_win11_style(self):
        try:
            self.iconbitmap(default="")
        except Exception:
            pass
        font_family = "Segoe UI Variable Text"
        if font_family not in tkfont.families():
            font_family = "Segoe UI"
        self.default_font = (font_family, 10)
        self.option_add("*Font", self.default_font)

        style = ttk.Style(self)
        try:
            style.configure("Treeview", rowheight=26, font=(font_family, 10))
            style.configure("Treeview.Heading", font=(font_family, 10, "bold"))
            style.configure("TButton", padding=6)
            style.configure("TEntry", padding=4)
        except Exception:
            pass
        bg = "#f3f3f3" if sv_ttk is None else None
        if bg:
            self.configure(bg=bg)

    # ---------------- UI ----------------
    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Pasta:").pack(side="left")
        self.folder_var = tk.StringVar(value=self.folder)
        self.folder_entry = ttk.Entry(top, textvariable=self.folder_var)
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.folder_entry.bind("<Return>", lambda e: self.apply_folder_from_entry())
        self.folder_entry.bind("<FocusOut>", lambda e: self.apply_folder_from_entry())

        ttk.Button(top, text="Escolher pasta...", command=self.choose_folder).pack(side="left", padx=2)
        self.index_btn = ttk.Button(top, text="Atualizar", command=self.start_indexing)
        self.index_btn.pack(side="left", padx=2)
        self.reindex_btn = ttk.Button(top, text="Reanalisar tudo", command=self.force_reindex_all)
        self.reindex_btn.pack(side="left", padx=2)
        self.cancel_btn = ttk.Button(top, text="Cancelar", command=self.cancel_indexing, state="disabled")
        self.cancel_btn.pack(side="left", padx=2)

        search_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        search_frame.pack(fill="x")

        ttk.Label(search_frame, text="Buscar palavra/trecho:").pack(side="left")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=6)
        search_entry.bind("<Return>", lambda e: self.do_search())
        search_entry.focus_set()

        ttk.Button(search_frame, text="Buscar", command=self.do_search).pack(side="left")

        self.status_var = tk.StringVar(value="Pronto.")
        status_bar = ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(10, 2))
        status_bar.pack(fill="x")

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=10)

        # resultados: 1 linha-pai por documento (copias identicas agrupadas),
        # com 1 linha-filha por pagina onde o termo aparece
        self.row_meta = {}  # iid -> {"all_paths": [...], "page": int|None}
        columns = ("ocorrencias", "trecho")
        self.tree = ttk.Treeview(self, columns=columns, show="tree headings")
        self.tree.heading("#0", text="Arquivo / Pagina")
        self.tree.heading("ocorrencias", text="Ocorrencias")
        self.tree.heading("trecho", text="Trecho")
        self.tree.column("#0", width=340)
        self.tree.column("ocorrencias", width=90, anchor="center")
        self.tree.column("trecho", width=500)
        self.tree.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.tree.bind("<Double-1>", self.open_selected)

        self.context_menu = tk.Menu(self.tree, tearoff=0)
        self.context_menu.add_command(label="Abrir PDF", command=lambda: self.open_selected(None))
        self.context_menu.add_command(label="Reanalisar este PDF", command=self.force_reindex_selected)
        self.tree.bind("<Button-3>", self._show_context_menu)

        ocr_msg = (
            f"OCR ativo ({OCR_LANG})." if OCR_AVAILABLE
            else "OCR indisponivel (Tesseract nao encontrado) - PDFs so-imagem nao serao lidos."
        )
        hint = ttk.Label(
            self,
            text=(
                "Dica: a analise nao comeca sozinha - edite o campo 'Pasta' (ou use 'Escolher pasta...') e so "
                "depois clique em 'Atualizar' para indexar. Cada documento pode ser expandido para ver as "
                "paginas onde o termo aparece. De dois cliques numa pagina para abrir o PDF direto nela "
                "(funciona com Foxit, Adobe Acrobat/Reader, PDF-XChange, SumatraPDF e Edge; em leitores nao "
                "reconhecidos o PDF abre normalmente, sem pular pagina). Copias identicas de um mesmo PDF "
                "aparecem como 1 unico documento. 'Atualizar' verifica PDFs novos/alterados e le so "
                f"esses (o resto fica salvo em {INDEX_FILENAME}). {ocr_msg}"
            ),
            padding=(10, 0, 10, 10),
            wraplength=1000,
        )
        hint.pack(fill="x")

    # ---------------- acoes ----------------
    def _set_folder(self, folder):
        """Define a pasta de analise sem iniciar a indexacao automaticamente -
        o usuario decide quando comecar, clicando em 'Atualizar'."""
        self.folder = folder
        self.folder_var.set(folder)
        self.index.close()
        self.index = PDFIndex(folder)
        save_last_folder(folder)
        self.tree.delete(*self.tree.get_children())
        self.row_meta = {}
        n = self.index.count()
        if n:
            msg = f'{n} PDF(s) no indice (cache carregado). Clique em "Atualizar" para verificar novidades.'
        else:
            msg = 'Pasta definida. Clique em "Atualizar" para iniciar a analise dos PDFs.'
        if self.index.used_fallback_dir:
            msg += " (pasta e so-leitura: indice sera salvo localmente, ao lado do programa)"
        self.status_var.set(msg)

    def choose_folder(self):
        if self.indexing:
            messagebox.showinfo(APP_TITLE, "Aguarde a indexacao atual terminar.")
            return
        folder = filedialog.askdirectory(initialdir=self.folder, title="Escolha a pasta com os PDFs")
        if folder:
            self._set_folder(folder)

    def apply_folder_from_entry(self):
        """Aplica a pasta digitada/colada diretamente no campo de texto."""
        if self.indexing:
            return
        folder = self.folder_var.get().strip()
        if not folder or os.path.normcase(os.path.abspath(folder)) == os.path.normcase(os.path.abspath(self.folder)):
            return
        if not os.path.isdir(folder):
            messagebox.showwarning(APP_TITLE, f"Pasta nao encontrada:\n{folder}")
            self.folder_var.set(self.folder)
            return
        self._set_folder(folder)

    def start_indexing(self, force=False):
        if self.indexing:
            return
        self.indexing = True
        self.stop_flag = threading.Event()
        self.index_btn.config(state="disabled")
        self.reindex_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress.config(value=0, maximum=100)
        threading.Thread(target=self._index_worker, args=(force,), daemon=True).start()

    def cancel_indexing(self):
        if self.indexing:
            self.stop_flag.set()
            self.status_var.set("Cancelando... (o que ja foi analisado fica salvo)")

    def force_reindex_all(self):
        if self.indexing:
            return
        if not messagebox.askyesno(
            APP_TITLE,
            "Isso vai reler TODOS os PDFs da pasta (incluindo OCR nos que forem so imagem). "
            "Pode demorar bastante dependendo da quantidade de arquivos. Continuar?",
        ):
            return
        self.start_indexing(force=True)

    def force_reindex_selected(self):
        if self.indexing:
            messagebox.showinfo(APP_TITLE, "Aguarde a indexacao atual terminar.")
            return
        sel = self.tree.selection()
        if not sel:
            return
        meta = self.row_meta.get(sel[0])
        if not meta:
            return
        for rel in meta["all_paths"]:
            self.index.forget(rel)
        self.start_indexing(force=False)

    def _show_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def _index_worker(self, force):
        def progress_cb(i, total, rel, cached, ocr):
            pct = int(i / total * 100) if total else 100
            self.after(0, self._set_progress, pct, f"Indexando ({i}/{total}): {rel}")

        self.after(0, self._set_status, "Procurando PDFs e indexando (isso e feito so uma vez por arquivo)...")
        t0 = time.time()
        try:
            self.index.rebuild(progress_cb=progress_cb, stop_flag=self.stop_flag, force=force)
        except Exception as e:
            self.after(0, lambda: messagebox.showerror(APP_TITLE, f"Erro ao indexar: {e}"))
        dt = time.time() - t0
        n = self.index.count()
        msg = f"Indexacao concluida: {n} PDF(s) em {dt:.1f}s."
        if self.index.used_fallback_dir:
            msg += " (indice salvo localmente - pasta de origem e so-leitura)"
        self.after(0, self._set_progress, 100, msg)
        self.after(0, self._finish_indexing)

    def _finish_indexing(self):
        self.indexing = False
        self.index_btn.config(state="normal")
        self.reindex_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")

    def _set_progress(self, pct, text):
        self.progress.config(value=pct)
        self.status_var.set(text)

    def _set_status(self, text):
        self.status_var.set(text)

    def _refresh_status_only(self):
        n = self.index.count()
        if n:
            msg = f"{n} PDF(s) no indice (cache carregado)."
            if self.index.used_fallback_dir:
                msg += " (pasta e so-leitura: indice salvo localmente, ao lado do programa)"
            self.status_var.set(msg)

    def do_search(self):
        query = self.search_var.get().strip()
        self.tree.delete(*self.tree.get_children())
        self.row_meta = {}
        if not query:
            return
        if self.indexing:
            messagebox.showinfo(APP_TITLE, "Ainda indexando, aguarde terminar para resultados completos.")
        results = self.index.search(query)
        if not results:
            self.status_var.set(f"Nenhum resultado para '{query}'.")
            return
        for r in results:
            nome = r["rel"]
            if len(r["all_paths"]) > 1:
                nome += f"  (+{len(r['all_paths']) - 1} copia(s) identica(s))"
            first_page = r["pages"][0][0] if r["pages"] else None
            parent = self.tree.insert("", "end", text=nome, values=(r["total"], ""), open=True)
            self.row_meta[parent] = {"all_paths": r["all_paths"], "page": first_page}
            for p, c, snippet in r["pages"]:
                child = self.tree.insert(parent, "end", text=f"    Pagina {p}", values=(c, snippet))
                self.row_meta[child] = {"all_paths": r["all_paths"], "page": p}
        total_ocorrencias = sum(r["total"] for r in results)
        self.status_var.set(
            f"{total_ocorrencias} ocorrencia(s) em {len(results)} documento(s) para '{query}'."
        )

    def open_selected(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        meta = self.row_meta.get(sel[0])
        if not meta:
            return
        full = None
        for rel in meta["all_paths"]:
            candidate = os.path.join(self.folder, rel)
            if os.path.exists(candidate):
                full = candidate
                break
        if not full:
            messagebox.showwarning(APP_TITLE, "Arquivo nao encontrado (pode ter sido movido/apagado).")
            return
        if not open_pdf_at_page(full, meta.get("page")):
            messagebox.showerror(APP_TITLE, "Nao foi possivel abrir o arquivo.")


if __name__ == "__main__":
    app = App()
    app.mainloop()
