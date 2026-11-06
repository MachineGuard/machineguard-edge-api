"""Sanitize text at the integration runner's publication boundary."""
import re
from pathlib import Path

GATEWAY=re.compile(r'mgg_[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\.[A-Za-z0-9_-]+')
NODE=re.compile(r'(?:mgn_|mgw_)[A-Za-z0-9_-]+')
JWT=re.compile(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+')


def sanitize_text(text,private=()):
    for value in private:
        if value:text=text.replace(value,'[REDACTED]')
    text=GATEWAY.sub('[CREDENTIAL_REDACTED]',text)
    text=NODE.sub('[CREDENTIAL_REDACTED]',text)
    return JWT.sub('[JWT_REDACTED]',text)


def redirect_report_path(value,report,staging):
    return re.sub(re.escape(str(report))+r'(?=[\\/]|$)',lambda _match:str(staging),str(value))


def publish_artifacts(staging,report,clean):
    """Producer files stay private until sanitized; never copy raw report bytes."""
    staging,report=Path(staging),Path(report)
    for source in staging.rglob('*'):
        if source.suffix.lower() not in ('.log','.xml','.json'):continue
        if source.is_symlink() or not source.resolve().is_relative_to(staging.resolve()):
            raise ValueError('Unsafe report artifact path')
        if not source.is_file():continue
        relative=Path(clean(str(source.relative_to(staging))))
        target=report/relative
        if not target.resolve().is_relative_to(report.resolve()):raise ValueError('Unsafe report destination')
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(clean(source.read_text(encoding='utf-8',errors='replace')),encoding='utf-8')
