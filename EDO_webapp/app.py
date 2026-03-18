from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, abort
from models import User, Document, ActionLog, Facsimile, DocumentNote, DocumentTemplate
from extensions import db
import os
from datetime import datetime, timedelta
from datetime import timezone
from zoneinfo import ZoneInfo
from werkzeug.utils import secure_filename
from PIL import Image
import io
import uuid
import fitz
import mimetypes
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
import json
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev-key'
app.config['SQLALCHEMY_DATABASE_URI'] = \
    'postgresql://postgres:1234@localhost:5432/edo_db'

db.init_app(app)

APP_TZ = ZoneInfo("Asia/Krasnoyarsk")

def _as_kras_time(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    # created_at is stored as naive UTC (datetime.utcnow)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(APP_TZ)

@app.template_filter("kras_dt")
def kras_dt_filter(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    local = _as_kras_time(dt)
    return local.strftime(fmt) if local else "—"

def log_action(*, action: str, message: str, user: User | None = None, document: Document | None = None):
    log = ActionLog(
        action=action,
        message=message,
        user_id=(user.id if user else None),
        username=(user.username if user else None),
        document_id=(document.id if document else None),
        document_title=(document.title if document else None),
    )
    db.session.add(log)
    db.session.commit()

def get_current_user() -> User | None:
    user_id = session.get('user_id')
    if not user_id:
        return None
    return User.query.get(user_id)

def _is_admin() -> bool:
    return session.get('user_role') == 'admin'

def require_admin() -> bool:
    if 'user_id' not in session:
        return False
    if not _is_admin():
        abort(403)
    return True

def _confirm_admin_password(password: str | None) -> bool:
    if not password:
        return False
    admin = get_current_user()
    if not admin:
        return False
    return admin.check_password(password)

def _file_ext(path: str | None) -> str:
    if not path:
        return ''
    _, ext = os.path.splitext(path)
    return ext.lower().lstrip('.')

def _can_preview_or_sign(doc: Document) -> bool:
    ext = _file_ext(doc.file_path)
    return ext in ('pdf', 'png', 'jpg', 'jpeg')

def _signature_to_rgba(signature_bytes: bytes) -> Image.Image:
    """
    Converts an input image to a cropped RGBA signature with transparent background.
    - Removes near-white background with a soft alpha based on luminance.
    - Crops empty margins around the signature.
    """
    img = Image.open(io.BytesIO(signature_bytes)).convert("RGBA")
    w, h = img.size

    # Create alpha from luminance (soft threshold)
    data = list(img.getdata())
    new = []
    for r, g, b, a in data:
        lum = int(0.299 * r + 0.587 * g + 0.114 * b)
        # Map luminance -> alpha: white-ish becomes transparent
        # tweakable: start fading at 210..255
        if lum >= 250:
            alpha = 0
        elif lum >= 210:
            alpha = int((250 - lum) / 40 * 255)
        else:
            alpha = 255
        # Keep original RGB, set computed alpha
        new.append((r, g, b, min(a, alpha)))

    img.putdata(new)

    # Crop to non-transparent bbox with padding
    bbox = img.getbbox()
    if bbox:
        pad = max(6, int(min(w, h) * 0.02))
        left = max(0, bbox[0] - pad)
        top = max(0, bbox[1] - pad)
        right = min(w, bbox[2] + pad)
        bottom = min(h, bbox[3] + pad)
        img = img.crop((left, top, right, bottom))

    return img

def _make_signed_filename(original_path: str) -> str:
    base = os.path.basename(original_path)
    name, ext = os.path.splitext(base)
    return f"{name}__signed__{uuid.uuid4().hex[:8]}{ext.lower()}"

def _ensure_uploads_dir() -> str:
    upload_folder = 'uploads'
    os.makedirs(upload_folder, exist_ok=True)
    return upload_folder

def _safe_key(key: str) -> str:
    key = (key or '').strip()
    out = []
    for ch in key:
        if ch.isalnum() or ch in ('_', '-'):
            out.append(ch)
    return ''.join(out)[:40]

def _render_template_text(base_text: str, values: dict[str, str]) -> str:
    text = base_text or ''
    for k, v in values.items():
        text = text.replace('{{' + k + '}}', v)
    return text

def _generate_pdf_from_template(*, title: str, rendered_text: str, out_path: str):
    # Ensure Cyrillic-capable fonts (Windows)
    def ensure_fonts():
        if getattr(ensure_fonts, "_done", False):
            return
        ensure_fonts._done = True

        font_candidates = [
            ("AppFont", r"C:\Windows\Fonts\arial.ttf"),
            ("AppFontBold", r"C:\Windows\Fonts\arialbd.ttf"),
            ("AppFont", r"C:\Windows\Fonts\calibri.ttf"),
            ("AppFontBold", r"C:\Windows\Fonts\calibrib.ttf"),
            ("AppFont", r"C:\Windows\Fonts\times.ttf"),
            ("AppFontBold", r"C:\Windows\Fonts\timesbd.ttf"),
        ]

        for name, path in font_candidates:
            try:
                if os.path.exists(path) and name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, path))
            except Exception:
                pass

    ensure_fonts()

    c = rl_canvas.Canvas(out_path, pagesize=A4)
    width, height = A4

    margin_x = 18 * mm
    y = height - 20 * mm

    title_font = "AppFontBold" if "AppFontBold" in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"
    body_font = "AppFont" if "AppFont" in pdfmetrics.getRegisteredFontNames() else "Helvetica"

    c.setFont(title_font, 14)
    c.drawString(margin_x, y, title)
    y -= 10 * mm

    c.setFont(body_font, 10)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    c.drawString(margin_x, y, datetime.now(APP_TZ).strftime("Сформировано: %d.%m.%Y %H:%M"))
    c.setFillColorRGB(0, 0, 0)
    y -= 12 * mm

    c.setFont(body_font, 11)
    max_width = width - margin_x * 2
    line_height = 6.0 * mm

    def wrap_line(s: str) -> list[str]:
        words = s.split(' ')
        lines = []
        cur = ''
        for w in words:
            test = (cur + ' ' + w).strip()
            if c.stringWidth(test, "Helvetica", 11) <= max_width:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines or ['']

    for paragraph in (rendered_text or '').splitlines():
        wrapped = wrap_line(paragraph)
        for ln in wrapped:
            if y <= 18 * mm:
                c.showPage()
                y = height - 20 * mm
                c.setFont(body_font, 11)
            c.drawString(margin_x, y, ln)
            y -= line_height
        y -= 2.5 * mm

    c.save()

def _parse_position(form_value: str) -> str:
    value = (form_value or 'br').lower()
    if value not in ('br', 'bl', 'tr', 'tl'):
        return 'br'
    return value

def _parse_size(form_value: str) -> str:
    value = (form_value or 'm').lower()
    if value not in ('s', 'm', 'l'):
        return 'm'
    return value

def _parse_int(value: str | None, default: int = 0, min_v: int = -9999, max_v: int = 9999) -> int:
    try:
        v = int(value) if value is not None and value != '' else default
    except Exception:
        v = default
    return max(min_v, min(max_v, v))

def _parse_float(value: str | None, default: float, min_v: float, max_v: float) -> float:
    try:
        v = float(value) if value is not None and value != '' else default
    except Exception:
        v = default
    return max(min_v, min(max_v, v))

@app.route('/facsimile/<int:facsimile_id>.png')
def facsimile_png(facsimile_id: int):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = get_current_user()
    fac = Facsimile.query.get_or_404(facsimile_id)
    if not user or fac.user_id != user.id:
        abort(403)
    return send_file(io.BytesIO(fac.image_png), mimetype='image/png')

@app.route('/facsimile/<int:facsimile_id>/delete', methods=['POST'])
def delete_facsimile(facsimile_id: int):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = get_current_user()
    fac = Facsimile.query.get_or_404(facsimile_id)
    if not user or fac.user_id != user.id:
        abort(403)

    return_doc_id = request.form.get('return_doc_id')
    db.session.delete(fac)
    db.session.commit()

    log_action(action='facsimile', message='Удалил сохранённое факсимиле', user=user)
    flash('Факсимиле удалено')

    if return_doc_id:
        try:
            return redirect(url_for('document_view', doc_id=int(return_doc_id)))
        except Exception:
            pass
    return redirect(url_for('home'))

@app.route('/document/<int:doc_id>/sign-preview.png')
def sign_preview(doc_id: int):
    """
    Returns a PNG preview used for interactive signature placement.
    - For PDF: renders last page via PyMuPDF.
    - For images: returns a resized PNG copy.
    """
    if 'user_id' not in session:
        return redirect(url_for('login'))

    doc = Document.query.get_or_404(doc_id)
    if not doc.file_path or not os.path.exists(doc.file_path):
        abort(404)

    path = doc.file_path
    _, ext = os.path.splitext(path.lower())

    try:
        if ext == '.pdf':
            pdf = fitz.open(path)
            page = pdf[-1]
            # Render at a reasonable resolution for UI (fast)
            mat = fitz.Matrix(2, 2)  # ~144 DPI
            pix = page.get_pixmap(matrix=mat, alpha=False)
            png_bytes = pix.tobytes('png')
            pdf.close()
            return send_file(io.BytesIO(png_bytes), mimetype='image/png')

        if ext in ('.png', '.jpg', '.jpeg'):
            img = Image.open(path).convert("RGBA")
            max_w = 980
            if img.width > max_w:
                new_h = int(img.height * (max_w / img.width))
                img = img.resize((max_w, max(1, new_h)))
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            return send_file(io.BytesIO(buf.getvalue()), mimetype='image/png')

    except Exception:
        abort(500)

    abort(415)

@app.route('/document/<int:doc_id>/sign', methods=['POST'])
def sign_document(doc_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    doc = Document.query.get_or_404(doc_id)
    user = get_current_user()

    if doc.is_deleted:
        flash('Нельзя подписывать документ из архива')
        return redirect(url_for('archive_document_view', doc_id=doc.id))

    if not doc.file_path or not os.path.exists(doc.file_path):
        flash('Файл документа не найден')
        return redirect(url_for('document_view', doc_id=doc.id))

    placement_mode = (request.form.get('placement_mode') or 'preset').lower()

    position = _parse_position(request.form.get('position'))
    size = _parse_size(request.form.get('size'))
    offset_x = _parse_int(request.form.get('offset_x'), default=0, min_v=-500, max_v=500)
    offset_y = _parse_int(request.form.get('offset_y'), default=0, min_v=-500, max_v=500)

    # For interactive mode: normalized coords (0..1) and width fraction
    norm_x = _parse_float(request.form.get('norm_x'), default=0.70, min_v=0.0, max_v=1.0)
    norm_y = _parse_float(request.form.get('norm_y'), default=0.80, min_v=0.0, max_v=1.0)
    norm_w = _parse_float(request.form.get('norm_w'), default=0.28, min_v=0.08, max_v=0.80)

    facsimile_id = request.form.get('facsimile_id')
    sig_rgba: Image.Image | None = None

    # 1) Prefer selecting an existing facsimile from DB
    if facsimile_id:
        try:
            fac_id = int(facsimile_id)
            fac = Facsimile.query.get(fac_id)
        except Exception:
            fac = None
        if fac and user and fac.user_id == user.id:
            sig_rgba = Image.open(io.BytesIO(fac.image_png)).convert("RGBA")

    # 2) Or upload a new one (and save to DB for next time)
    if sig_rgba is None:
        sig_file = request.files.get('signature')
        if not sig_file or not sig_file.filename:
            flash('Выберите сохранённое факсимиле или загрузите изображение подписи')
            return redirect(url_for('document_view', doc_id=doc.id))

        sig_bytes = sig_file.read()
        try:
            sig_rgba = _signature_to_rgba(sig_bytes)
        except Exception:
            flash('Не удалось прочитать изображение подписи (нужен PNG/JPG/JPEG)')
            return redirect(url_for('document_view', doc_id=doc.id))

        if user:
            buf = io.BytesIO()
            sig_rgba.save(buf, format='PNG')
            fac = Facsimile(
                user_id=user.id,
                name='Факсимиле',
                image_png=buf.getvalue()
            )
            db.session.add(fac)
            db.session.commit()

    upload_folder = _ensure_uploads_dir()
    original_path = doc.file_path
    _, ext = os.path.splitext(original_path.lower())

    signed_filename = _make_signed_filename(original_path)
    signed_path = os.path.join(upload_folder, signed_filename)

    try:
        if ext == '.pdf':
            # Stamp signature on the last page.
            pdf = fitz.open(original_path)
            page = pdf[-1]

            page_rect = page.rect
            if placement_mode == 'free':
                target_w = page_rect.width * norm_w
            else:
                size_map = {'s': 0.18, 'm': 0.28, 'l': 0.38}
                target_w = min(220, page_rect.width * size_map.get(size, 0.28))
            target_h = target_w * (sig_rgba.height / max(sig_rgba.width, 1))
            target_h = min(target_h, 80)

            if placement_mode == 'free':
                x0 = page_rect.x0 + (page_rect.width * norm_x)
                y0 = page_rect.y0 + (page_rect.height * norm_y)
                # Clamp to page
                x0 = max(page_rect.x0, min(page_rect.x1 - target_w, x0))
                y0 = max(page_rect.y0, min(page_rect.y1 - target_h, y0))
                rect = fitz.Rect(x0, y0, x0 + target_w, y0 + target_h)
            else:
                margin = 24
                if position == 'br':
                    x1 = page_rect.x1 - margin + offset_x
                    y1 = page_rect.y1 - margin + offset_y
                    rect = fitz.Rect(x1 - target_w, y1 - target_h, x1, y1)
                elif position == 'bl':
                    x0 = page_rect.x0 + margin + offset_x
                    y1 = page_rect.y1 - margin + offset_y
                    rect = fitz.Rect(x0, y1 - target_h, x0 + target_w, y1)
                elif position == 'tr':
                    x1 = page_rect.x1 - margin + offset_x
                    y0 = page_rect.y0 + margin + offset_y
                    rect = fitz.Rect(x1 - target_w, y0, x1, y0 + target_h)
                else:  # tl
                    x0 = page_rect.x0 + margin + offset_x
                    y0 = page_rect.y0 + margin + offset_y
                    rect = fitz.Rect(x0, y0, x0 + target_w, y0 + target_h)

            buf = io.BytesIO()
            sig_rgba.save(buf, format='PNG')
            page.insert_image(rect, stream=buf.getvalue(), keep_proportion=True, overlay=True)
            pdf.save(signed_path)
            pdf.close()

        elif ext in ('.png', '.jpg', '.jpeg'):
            base = Image.open(original_path).convert("RGBA")

            if placement_mode == 'free':
                target_w = int(base.width * norm_w)
            else:
                size_map = {'s': 0.22, 'm': 0.32, 'l': 0.42}
                target_w = int(min(320, base.width * size_map.get(size, 0.32)))
            target_h = int(target_w * (sig_rgba.height / max(sig_rgba.width, 1)))
            sig_resized = sig_rgba.resize((max(1, target_w), max(1, target_h)))

            if placement_mode == 'free':
                x = int(base.width * norm_x)
                y = int(base.height * norm_y)
            else:
                margin = int(max(12, base.width * 0.02))
                if position == 'br':
                    x = base.width - margin - sig_resized.width + offset_x
                    y = base.height - margin - sig_resized.height + offset_y
                elif position == 'bl':
                    x = margin + offset_x
                    y = base.height - margin - sig_resized.height + offset_y
                elif position == 'tr':
                    x = base.width - margin - sig_resized.width + offset_x
                    y = margin + offset_y
                else:  # tl
                    x = margin + offset_x
                    y = margin + offset_y

            x = max(0, min(base.width - sig_resized.width, x))
            y = max(0, min(base.height - sig_resized.height, y))

            base.alpha_composite(sig_resized, (x, y))

            # Save with original-ish format
            if ext == '.png':
                base.save(signed_path, format='PNG')
            else:
                base.convert("RGB").save(signed_path, format='JPEG', quality=92)

        else:
            flash('Подпись поддерживается только для PDF/PNG/JPG/JPEG')
            return redirect(url_for('document_view', doc_id=doc.id))

    except Exception as e:
        flash(f'Не удалось подписать документ: {e}')
        return redirect(url_for('document_view', doc_id=doc.id))

    # Create a signed copy as a NEW document (original stays unchanged)
    ext_only = _file_ext(signed_path)
    signed_title = doc.title
    if '(подпис' not in signed_title.lower():
        signed_title = f"{signed_title} (подписанный)"

    signed_doc = Document(
        title=signed_title,
        doc_type=(ext_only.upper() if ext_only else (doc.doc_type or 'Файл')),
        file_path=signed_path,
        author_id=session['user_id']
    )
    db.session.add(signed_doc)
    db.session.commit()

    if user:
        log_action(
            action='sign',
            message=f'Подписал документ «{doc.title}» и создал копию «{signed_doc.title}»',
            user=user,
            document=signed_doc
        )

    flash('Документ подписан: создана копия (подписанный документ)')
    return redirect(url_for('document_view', doc_id=signed_doc.id))

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    q = (request.args.get('q') or '').strip()
    fmt = (request.args.get('fmt') or '').strip().lower()
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()

    query = Document.query.filter_by(is_deleted=False)

    if q:
        query = query.filter(Document.title.ilike(f"%{q}%"))

    if date_from:
        try:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d")
            query = query.filter(Document.created_at >= dt_from)
        except Exception:
            pass

    if date_to:
        try:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(Document.created_at < dt_to)
        except Exception:
            pass

    if fmt and fmt != 'all':
        if fmt in ('pdf', 'png', 'jpg', 'jpeg'):
            query = query.filter(Document.file_path.ilike(f"%.{fmt}"))
        elif fmt == 'none':
            query = query.filter(or_(Document.file_path == None, Document.file_path == ''))  # noqa: E711

    documents = query.order_by(Document.created_at.desc()).all()
    return render_template(
        'index.html',
        documents=documents,
        q=q,
        fmt=(fmt or 'all'),
        date_from=date_from,
        date_to=date_to
    )

@app.route('/home')
def home():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    return render_template('home.html')

@app.route('/document/<int:doc_id>')
def document_view(doc_id):
    doc = Document.query.get_or_404(doc_id)
    author = User.query.get(doc.author_id)
    file_name = os.path.basename(doc.file_path) if doc.file_path else None
    user = get_current_user()
    facsimiles = Facsimile.query.filter_by(user_id=user.id).order_by(Facsimile.created_at.desc()).all() if user else []
    notes = DocumentNote.query.filter_by(document_id=doc.id).order_by(DocumentNote.created_at.desc()).all()
    if user:
        log_action(
            action='view',
            message=f'Открыл документ «{doc.title}»',
            user=user,
            document=doc
        )

    return render_template(
        'document.html',
        doc=doc,
        author=author,
        file_name=file_name,
        facsimiles=facsimiles,
        notes=notes
    )

@app.route('/document/<int:doc_id>/note', methods=['POST'])
def add_note(doc_id: int):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    doc = Document.query.get_or_404(doc_id)
    user = get_current_user()
    text = (request.form.get('text') or '').strip()
    if not text:
        flash('Введите текст примечания')
        return redirect(url_for('document_view', doc_id=doc.id))
    note = DocumentNote(
        document_id=doc.id,
        user_id=(user.id if user else None),
        username=(user.username if user else None),
        text=text[:800]
    )
    db.session.add(note)
    db.session.commit()
    if user:
        log_action(action='note', message=f'Добавил примечание к документу «{doc.title}»', user=user, document=doc)
    return redirect(url_for('document_view', doc_id=doc.id))

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        user = get_current_user()
        title = request.form['title']
        # Тип при загрузке не выбираем (упрощённая форма)
        doc_type = request.form.get('doc_type') or 'Файл'
        file = request.files['file']

        filename = secure_filename(file.filename)
        upload_folder = 'uploads'
        os.makedirs(upload_folder, exist_ok=True)

        file_path = os.path.join(upload_folder, filename)
        file.save(file_path)

        document = Document(
            title=title,
            doc_type=doc_type,
            file_path=file_path,
            author_id=session['user_id']
        )

        db.session.add(document)
        db.session.commit()

        if user:
            log_action(
                action='upload',
                message=f'Загрузил файл и создал документ «{document.title}»',
                user=user,
                document=document
            )
        return redirect(url_for('home'))
    return render_template('upload.html')


@app.route('/document/<int:doc_id>/send')
def send_to_workflow(doc_id):
    doc = Document.query.get_or_404(doc_id)
    doc.status = 'На согласовании'
    db.session.commit()
    user = get_current_user()
    if user:
        log_action(
            action='send',
            message=f'Отправил документ «{doc.title}» на согласование',
            user=user,
            document=doc
        )
    return redirect(url_for('document_view', doc_id=doc.id))

@app.route('/document/<int:doc_id>/download')
def download_document(doc_id):
    doc = Document.query.get_or_404(doc_id)
    user = get_current_user()
    if user:
        log_action(
            action='download',
            message=f'Скачал документ «{doc.title}»',
            user=user,
            document=doc
        )
    return send_file(doc.file_path, as_attachment=True)

@app.route('/archive')
def archive():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    q = (request.args.get('q') or '').strip()
    fmt = (request.args.get('fmt') or '').strip().lower()
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()

    query = Document.query.filter_by(is_deleted=True)
    if q:
        query = query.filter(Document.title.ilike(f"%{q}%"))
    if date_from:
        try:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d")
            query = query.filter(Document.created_at >= dt_from)
        except Exception:
            pass
    if date_to:
        try:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(Document.created_at < dt_to)
        except Exception:
            pass
    if fmt and fmt != 'all':
        if fmt in ('pdf', 'png', 'jpg', 'jpeg'):
            query = query.filter(Document.file_path.ilike(f"%.{fmt}"))
        elif fmt == 'none':
            query = query.filter(or_(Document.file_path == None, Document.file_path == ''))  # noqa: E711

    documents = query.order_by(Document.created_at.desc()).all()
    return render_template(
        'archive.html',
        documents=documents,
        q=q,
        fmt=(fmt or 'all'),
        date_from=date_from,
        date_to=date_to
    )

@app.route('/archive/document/<int:doc_id>')
def archive_document_view(doc_id: int):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    doc = Document.query.get_or_404(doc_id)
    if not doc.is_deleted:
        return redirect(url_for('document_view', doc_id=doc.id))
    author = User.query.get(doc.author_id)
    file_name = os.path.basename(doc.file_path) if doc.file_path else None
    notes = DocumentNote.query.filter_by(document_id=doc.id).order_by(DocumentNote.created_at.desc()).all()
    user = get_current_user()
    if user:
        log_action(action='view', message=f'Открыл архивный документ «{doc.title}»', user=user, document=doc)
    return render_template(
        'archive_document.html',
        doc=doc,
        author=author,
        file_name=file_name,
        notes=notes
    )

@app.route('/archive/document/<int:doc_id>/note', methods=['POST'])
def add_note_archive(doc_id: int):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    doc = Document.query.get_or_404(doc_id)
    if not doc.is_deleted:
        return redirect(url_for('document_view', doc_id=doc.id))
    user = get_current_user()
    text = (request.form.get('text') or '').strip()
    if not text:
        flash('Введите текст примечания')
        return redirect(url_for('archive_document_view', doc_id=doc.id))
    note = DocumentNote(
        document_id=doc.id,
        user_id=(user.id if user else None),
        username=(user.username if user else None),
        text=text[:800]
    )
    db.session.add(note)
    db.session.commit()
    if user:
        log_action(action='note', message=f'Добавил примечание к архивному документу «{doc.title}»', user=user, document=doc)
    return redirect(url_for('archive_document_view', doc_id=doc.id))

@app.route('/archive/document/<int:doc_id>/purge', methods=['POST'])
def purge_document(doc_id: int):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    doc = Document.query.get_or_404(doc_id)
    if not doc.is_deleted:
        flash('Документ не в архиве')
        return redirect(url_for('document_view', doc_id=doc.id))

    user = get_current_user()

    # Try to delete file from disk (optional)
    if doc.file_path and os.path.exists(doc.file_path):
        try:
            os.remove(doc.file_path)
        except Exception:
            pass

    # delete notes first
    DocumentNote.query.filter_by(document_id=doc.id).delete()

    # keep audit log rows, but remove FK references to the document
    ActionLog.query.filter_by(document_id=doc.id).update(
        {
            ActionLog.document_id: None,
            ActionLog.document_title: None,
        },
        synchronize_session=False
    )

    db.session.delete(doc)
    db.session.commit()

    if user:
        log_action(action='purge', message=f'Удалил документ «{doc.title}» навсегда', user=user)
    flash('Документ удалён навсегда')
    return redirect(url_for('archive'))

@app.route('/search')
def search():
    return redirect(url_for('index'))

@app.route('/logs')
def logs():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    items = ActionLog.query.order_by(ActionLog.created_at.desc()).limit(200).all()
    return render_template('logs.html', items=items)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            session['user_id'] = user.id
            session['user_role'] = user.role
            log_action(
                action='login',
                message='Вошёл в систему',
                user=user
            )
            return redirect(url_for('home'))
        else:
            flash('Неверный логин или пароль')

    return render_template('login.html')

@app.route('/document/<int:doc_id>/delete')
def delete_document(doc_id):
    doc = Document.query.get_or_404(doc_id)
    doc.is_deleted = True
    db.session.commit()
    user = get_current_user()
    if user:
        log_action(
            action='archive',
            message=f'Переместил документ «{doc.title}» в архив',
            user=user,
            document=doc
        )
    return redirect(url_for('home'))

from flask import send_from_directory

@app.route('/files/<path:filename>')
def view_file(filename):
    return send_from_directory('uploads', filename)


@app.route('/logout')
def logout():
    user = get_current_user()
    if user:
        log_action(
            action='logout',
            message='Вышел из системы',
            user=user
        )
    session.clear()
    return redirect(url_for('login'))


@app.route('/admin/users', methods=['GET'])
def admin_users():
    require_admin()
    users = User.query.order_by(User.id.asc()).all()
    return render_template('admin_users.html', users=users)


@app.route('/admin/users/create', methods=['POST'])
def admin_users_create():
    require_admin()

    admin_password = request.form.get('admin_password')
    if not _confirm_admin_password(admin_password):
        flash('Неверный пароль администратора')
        return redirect(url_for('admin_users'))

    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    role = (request.form.get('role') or 'user').strip().lower()

    if not username or not password:
        flash('Заполните логин и пароль нового пользователя')
        return redirect(url_for('admin_users'))

    if role not in ('user', 'admin'):
        role = 'user'

    user = User(username=username[:100], role=role)
    user.set_password(password)

    try:
        db.session.add(user)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('Пользователь с таким логином уже существует')
        return redirect(url_for('admin_users'))

    admin = get_current_user()
    if admin:
        log_action(action='user_create', message=f'Создал пользователя «{user.username}» (роль: {user.role})', user=admin)
    flash('Пользователь создан')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
def admin_users_delete(user_id: int):
    require_admin()

    admin_password = request.form.get('admin_password')
    if not _confirm_admin_password(admin_password):
        flash('Неверный пароль администратора')
        return redirect(url_for('admin_users'))

    current = get_current_user()
    if current and current.id == user_id:
        flash('Нельзя удалить самого себя')
        return redirect(url_for('admin_users'))

    user = User.query.get_or_404(user_id)

    # Prevent locking out the system: keep at least one admin
    if user.role == 'admin':
        admins_count = User.query.filter_by(role='admin').count()
        if admins_count <= 1:
            flash('Нельзя удалить последнего администратора')
            return redirect(url_for('admin_users'))

    try:
        db.session.delete(user)
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('Не удалось удалить пользователя')
        return redirect(url_for('admin_users'))

    if current:
        log_action(action='user_delete', message=f'Удалил пользователя «{user.username}»', user=current)
    flash('Пользователь удалён')
    return redirect(url_for('admin_users'))


@app.route('/create', methods=['GET', 'POST'])
def create():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = get_current_user()
    templates = DocumentTemplate.query.order_by(DocumentTemplate.created_at.desc()).all()
    templates_payload = [{'id': t.id, 'name': t.name, 'fields': t.fields_json, 'base_text': t.base_text} for t in templates]

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()

        if action == 'save_template':
            name = (request.form.get('template_name') or '').strip()
            base_text = (request.form.get('base_text') or '').strip()
            fields_raw = (request.form.get('fields_json') or '[]').strip()

            if not name or not base_text:
                flash('Заполните название шаблона и основной текст')
                return redirect(url_for('create'))

            try:
                fields = json.loads(fields_raw)
                if not isinstance(fields, list):
                    raise ValueError("fields not list")
            except Exception:
                flash('Ошибка: некорректный JSON полей')
                return redirect(url_for('create'))

            normalized = []
            for f in fields[:50]:
                if not isinstance(f, dict):
                    continue
                key = _safe_key(str(f.get('key', '')))
                label = str(f.get('label', '')).strip()[:80]
                default = str(f.get('default', '')).strip()[:200]
                if not key or not label:
                    continue
                normalized.append({'key': key, 'label': label, 'default': default})

            tpl = DocumentTemplate(
                name=name[:160],
                base_text=base_text,
                fields_json=json.dumps(normalized, ensure_ascii=False),
                author_id=(user.id if user else None),
                author_username=(user.username if user else None)
            )
            db.session.add(tpl)
            db.session.commit()
            flash('Шаблон сохранён')
            if user:
                log_action(action='template', message=f'Создал шаблон «{tpl.name}»', user=user)
            return redirect(url_for('create'))

        if action == 'update_template':
            template_id = request.form.get('template_id')
            name = (request.form.get('template_name') or '').strip()
            base_text = (request.form.get('base_text') or '').strip()
            fields_raw = (request.form.get('fields_json') or '[]').strip()

            if not template_id:
                flash('Выберите шаблон для редактирования')
                return redirect(url_for('create'))
            if not name or not base_text:
                flash('Заполните название шаблона и основной текст')
                return redirect(url_for('create'))

            tpl = DocumentTemplate.query.get(int(template_id))
            if not tpl:
                flash('Шаблон не найден')
                return redirect(url_for('create'))

            try:
                fields = json.loads(fields_raw)
                if not isinstance(fields, list):
                    raise ValueError("fields not list")
            except Exception:
                flash('Ошибка: некорректный JSON полей')
                return redirect(url_for('create'))

            normalized = []
            for f in fields[:50]:
                if not isinstance(f, dict):
                    continue
                key = _safe_key(str(f.get('key', '')))
                label = str(f.get('label', '')).strip()[:80]
                default = str(f.get('default', '')).strip()[:200]
                if not key or not label:
                    continue
                normalized.append({'key': key, 'label': label, 'default': default})

            tpl.name = name[:160]
            tpl.base_text = base_text
            tpl.fields_json = json.dumps(normalized, ensure_ascii=False)
            db.session.commit()

            flash('Шаблон обновлён')
            if user:
                log_action(action='template_edit', message=f'Отредактировал шаблон «{tpl.name}»', user=user)
            return redirect(url_for('create'))

        if action == 'create_from_template':
            template_id = request.form.get('template_id')
            title = (request.form.get('doc_title') or '').strip()

            if not template_id or not title:
                flash('Выберите шаблон и укажите название документа')
                return redirect(url_for('create'))

            tpl = DocumentTemplate.query.get(int(template_id))
            if not tpl:
                flash('Шаблон не найден')
                return redirect(url_for('create'))

            try:
                fields = json.loads(tpl.fields_json or '[]')
            except Exception:
                fields = []

            values: dict[str, str] = {}
            for f in fields:
                key = str(f.get('key', ''))
                default = str(f.get('default', ''))
                values[key] = (request.form.get(f'field_{key}') or '').strip() or default

            rendered = _render_template_text(tpl.base_text, values)

            upload_folder = _ensure_uploads_dir()
            safe_name = secure_filename(title) or 'document'
            out_name = f"{safe_name}__{uuid.uuid4().hex[:8]}.pdf"
            out_path = os.path.join(upload_folder, out_name)
            _generate_pdf_from_template(title=title, rendered_text=rendered, out_path=out_path)

            document = Document(
                title=title,
                doc_type='PDF',
                file_path=out_path,
                author_id=session['user_id']
            )
            db.session.add(document)
            db.session.commit()

            if user:
                log_action(
                    action='create',
                    message=f'Создал документ «{document.title}» по шаблону «{tpl.name}»',
                    user=user,
                    document=document
                )

            return redirect(url_for('document_view', doc_id=document.id))

    return render_template('create.html', templates=templates, templates_payload=templates_payload)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)