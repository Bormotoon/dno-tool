"""Кодировки и окончания строк удалённых текстовых файлов без потери байтов."""

from dataclasses import dataclass
import codecs
import re

MAX_REMOTE_TEXT_BYTES = 10 * 1024 * 1024
ENCODINGS = ('utf-8', 'utf-8-sig', 'utf-16-le', 'utf-16-be', 'cp1251')


@dataclass(frozen=True)
class RemoteTextFormat:
    encoding: str
    bom: bytes
    newline: str | None

    @classmethod
    def decode(cls, data: bytes, encoding: str | None = None) -> tuple[str, 'RemoteTextFormat']:
        bom = b''
        for prefix, codec in ((codecs.BOM_UTF8, 'utf-8'),
                              (codecs.BOM_UTF16_LE, 'utf-16-le'),
                              (codecs.BOM_UTF16_BE, 'utf-16-be')):
            if data.startswith(prefix):
                bom, encoding = prefix, codec
                data = data[len(prefix):]
                break
        encoding = encoding or 'utf-8'
        if encoding == 'utf-8-sig':
            encoding, bom = 'utf-8', codecs.BOM_UTF8
        text = data.decode(encoding, errors='strict')
        endings = set(re.findall(r'\r\n|\r|\n', text))
        newline = next(iter(endings)) if len(endings) == 1 else ('\n' if not endings else None)
        return text.replace('\r\n', '\n').replace('\r', '\n'), cls(encoding, bom, newline)

    def encode(self, text: str) -> bytes:
        if self.newline is None:
            raise ValueError('Выберите единый формат окончаний строк')
        return self.bom + text.replace('\n', self.newline).encode(self.encoding, errors='strict')
