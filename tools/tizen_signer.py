"""
Tizen Widget Signer (W3C XML Digital Signatures for Widgets, as used by Tizen Studio).

The TV rejects unsigned packages ("install failed[118, -12], Check certificate error"). A TV in
Developer Mode accepts packages signed with the Tizen test certificates from Tizen Studio (an author
certificate from Certificate Manager + the public distributor certificate) for `vd_appinstall`.
"""
import base64
import hashlib
import os
from typing import Dict, List, Tuple
from urllib.parse import quote

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import Encoding, pkcs12

AUTHOR_SIG = "author-signature.xml"
DISTRIBUTOR_SIG = "signature1.xml"

DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _digest(data: bytes) -> str:
    return _b64(hashlib.sha256(data).digest())


def _load_p12(path: str, password: bytes):
    with open(path, "rb") as f:
        key, cert, chain = pkcs12.load_key_and_certificates(f.read(), password)
    return key, [cert] + list(chain or [])


def _signature_xml(sig_id: str, role: str, files: List[Tuple[str, bytes]], key, certs) -> bytes:
    # The XML is written directly in canonical form, so the canonicalised SignedInfo / Object
    # (exc-c14n and c14n11) are the same text plus the inherited default namespace declaration.
    obj_inner = (
        '<SignatureProperties xmlns:dsp="http://www.w3.org/2009/xmldsig-properties">'
        f'<SignatureProperty Id="profile" Target="#{sig_id}">'
        '<dsp:Profile URI="http://www.w3.org/ns/widgets-digsig#profile"></dsp:Profile></SignatureProperty>'
        f'<SignatureProperty Id="role" Target="#{sig_id}">'
        f'<dsp:Role URI="http://www.w3.org/ns/widgets-digsig#role-{role}"></dsp:Role></SignatureProperty>'
        f'<SignatureProperty Id="identifier" Target="#{sig_id}">'
        '<dsp:Identifier></dsp:Identifier></SignatureProperty>'
        '</SignatureProperties>'
    )
    obj_c14n = f'<Object xmlns="{DSIG_NS}" Id="prop">{obj_inner}</Object>'

    refs = "".join(
        f'<Reference URI="{quote(name, safe="/")}">\n'
        f'<DigestMethod Algorithm="{SHA256}"></DigestMethod>\n'
        f'<DigestValue>{_digest(data)}</DigestValue>\n'
        '</Reference>\n'
        for name, data in files
    )
    signed_info_inner = (
        '\n<CanonicalizationMethod Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"></CanonicalizationMethod>\n'
        '<SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"></SignatureMethod>\n'
        f'{refs}'
        '<Reference URI="#prop">\n'
        '<Transforms>\n'
        '<Transform Algorithm="http://www.w3.org/2006/12/xml-c14n11"></Transform>\n'
        '</Transforms>\n'
        f'<DigestMethod Algorithm="{SHA256}"></DigestMethod>\n'
        f'<DigestValue>{_digest(obj_c14n.encode("utf-8"))}</DigestValue>\n'
        '</Reference>\n'
    )
    signed_info_c14n = f'<SignedInfo xmlns="{DSIG_NS}">{signed_info_inner}</SignedInfo>'
    signature = key.sign(signed_info_c14n.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())

    x509 = "".join(
        f'<X509Certificate>\n{_b64(c.public_bytes(Encoding.DER))}\n</X509Certificate>\n' for c in certs
    )
    xml = (
        f'<Signature xmlns="{DSIG_NS}" Id="{sig_id}">\n'
        f'<SignedInfo>{signed_info_inner}</SignedInfo>\n'
        f'<SignatureValue>\n{_b64(signature)}\n</SignatureValue>\n'
        f'<KeyInfo>\n<X509Data>\n{x509}</X509Data>\n</KeyInfo>\n'
        f'<Object Id="prop">{obj_inner}</Object>\n'
        '</Signature>'
    )
    return xml.encode("utf-8")


def sign_widget(files: Dict[str, bytes], cert_dir: str) -> Dict[str, bytes]:
    """
    Returns {author-signature.xml, signature1.xml} for the given package files
    ({archive path with '/' separators: content}). cert_dir holds author.p12, distributor.p12
    and password.txt: one line (same password for both) or two lines (author, distributor).
    """
    with open(os.path.join(cert_dir, "password.txt"), "rb") as f:
        lines = [l.strip() for l in f.read().splitlines() if l.strip()]
    author_pw, dist_pw = (lines + lines)[:2] if lines else (b"", b"")
    author_key, author_certs = _load_p12(os.path.join(cert_dir, "author.p12"), author_pw)
    dist_key, dist_certs = _load_p12(os.path.join(cert_dir, "distributor.p12"), dist_pw)

    content = sorted(
        (name, data) for name, data in files.items()
        if name not in (AUTHOR_SIG, DISTRIBUTOR_SIG) and not name.startswith("signature")
    )
    author_xml = _signature_xml("AuthorSignature", "author", content, author_key, author_certs)
    # The distributor signature also covers the author signature
    dist_files = sorted(content + [(AUTHOR_SIG, author_xml)])
    dist_xml = _signature_xml("DistributorSignature", "distributor", dist_files, dist_key, dist_certs)
    return {AUTHOR_SIG: author_xml, DISTRIBUTOR_SIG: dist_xml}
