// QR codes for the TOTP key and invite links, as a data: URL (CSP allows img-src data:).
import qrcode from 'qrcode-generator';

export function qrDataUrl(text: string, cell = 4): string {
	const qr = qrcode(0, 'M');
	qr.addData(text);
	qr.make();
	return qr.createDataURL(cell, cell * 2);
}

/** Group a base32 key as ABCD-EFGH-… for manual entry. */
export function groupKey(secret: string): string {
	return (secret.match(/.{1,4}/g) ?? []).join('-');
}
