import { createHash } from 'node:crypto';
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

// Mandatory end-to-end demo workflow against the real backend with test-only providers.
// Every step asserts; there are no optional branches.

const sha256 = (b: Buffer) => createHash('sha256').update(b).digest('hex');

interface QBState {
  bills: Array<{ Id: string; DocNumber: string; TotalAmt: number; Balance: number; VendorRef: { value: string } }>;
  attachments: Array<{ Id: string; sha256: string; AttachableRef: Array<{ EntityRef: { value: string } }> }>;
  create_bill_calls: number;
  upload_calls: number;
}

async function qbState(request: APIRequestContext): Promise<QBState> {
  const r = await request.get('/__test__/quickbooks');
  expect(r.ok()).toBeTruthy();
  return r.json();
}

async function fixture(request: APIRequestContext, key: string): Promise<Buffer> {
  const r = await request.get(`/__test__/fixtures/${key}`);
  expect(r.ok()).toBeTruthy();
  return Buffer.from(await r.body());
}

async function uploadBill(page: Page, name: string, buffer: Buffer) {
  await page.goto('/');
  await page.getByLabel('Upload bill').setInputFiles({ name, mimeType: 'application/pdf', buffer });
  const notice = page.getByRole('status').filter({ has: page.getByRole('link', { name: 'Open document' }) });
  await expect(notice).toBeVisible();
  return notice;
}

test('email import, approval, phone re-upload, history answer, unclear property, existing QuickBooks bill', async ({ page, request }) => {
  const billA = await fixture(request, 'A');
  const billB = await fixture(request, 'B');
  const billC = await fixture(request, 'C');
  const start = await qbState(request);
  expect(start.bills.filter((b) => b.DocNumber === 'NES-5531')).toHaveLength(1); // C entered manually beforehand
  expect(start.bills.filter((b) => b.DocNumber === 'MPL-2026-0415')).toHaveLength(0);

  // --- Sign in -----------------------------------------------------------
  await page.goto('/');
  await page.getByLabel('Username').fill('owner');
  await page.getByLabel('Password').fill('e2e-owner-password');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();

  // --- Import the labeled vendor email (twice: second is a no-op) ---------
  await page.getByRole('button', { name: 'Import demo emails' }).click();
  await expect(page.getByRole('status', { name: 'Import results' })).toContainText('Imported: 1');
  await page.getByRole('button', { name: 'Import demo emails' }).click();
  await expect(page.getByRole('status', { name: 'Import results' })).toContainText('already processed: 1');
  const documents = page.getByRole('list', { name: 'Documents' });
  await expect(documents.getByRole('link')).toHaveCount(1);
  const rowA = documents.getByRole('link', { name: /Metro Power & Light/ });
  await expect(rowA).toContainText('Ready for Approval');
  await rowA.click();
  await expect(page).toHaveURL(/\/documents\/[0-9a-f-]+$/);
  const docA = page.url().split('/documents/')[1];

  // --- Open the original and check it is the untouched email attachment ----
  const originalHref = await page.getByRole('region', { name: 'Original document' })
    .getByRole('link', { name: 'Open original' }).getAttribute('href');
  expect(originalHref).toBe(`/api/documents/${docA}/file`);
  const original = await page.request.get(originalHref!);
  expect(original.status()).toBe(200);
  expect(sha256(Buffer.from(await original.body()))).toBe(sha256(billA));

  // --- Review the extracted fields ----------------------------------------
  const details = page.getByRole('region', { name: 'Bill details' });
  await expect(details).toContainText('Metro Power & Light');
  await expect(details).toContainText('MPL-2026-0415');
  await expect(details).toContainText('142.50');
  await expect(details).toContainText('USD');
  await expect(details).toContainText('Demo Property');
  await expect(details).toContainText('Utilities');
  await expect(details.getByText('Unknown — needs review')).toHaveCount(0);

  // --- Approve once via the explicit confirmation --------------------------
  await page.getByRole('button', { name: 'Review and approve' }).click();
  const confirm = page.getByRole('region', { name: 'Confirm unpaid bill' });
  await expect(confirm).toContainText('Sandbox Test Company (TEST-REALM)');
  await confirm.getByRole('button', { name: 'Create unpaid bill' }).click();
  await expect(page.getByText(/Unpaid bill MPL-2026-0415 created with the original attached/)).toBeVisible();
  await expect(page.getByText('Recorded (Unpaid Bill)').first()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Review and approve' })).toHaveCount(0);

  let qb = await qbState(request);
  const billsA = qb.bills.filter((b) => b.DocNumber === 'MPL-2026-0415');
  expect(billsA).toHaveLength(1);
  expect(billsA[0].Balance).toBe(billsA[0].TotalAmt); // unpaid
  expect(billsA[0].TotalAmt).toBe(142.5);
  const attA = qb.attachments.filter((a) => a.AttachableRef[0].EntityRef.value === billsA[0].Id);
  expect(attA).toHaveLength(1);
  expect(attA[0].sha256).toBe(sha256(billA)); // the correct PDF
  expect(qb.create_bill_calls).toBe(1);

  // --- Phone: upload the same bytes under a different filename -------------
  await page.setViewportSize({ width: 390, height: 844 });
  const phoneNotice = await uploadBill(page, 'IMG_0412-phone-scan.pdf', billA);
  await expect(phoneNotice).toContainText('already stored');
  await phoneNotice.getByRole('link', { name: 'Open document' }).click();
  await expect(page).toHaveURL(new RegExp(`/documents/${docA}$`));
  const arrivals = page.getByRole('region', { name: /Arrivals/ });
  await expect(page.getByRole('heading', { name: 'Arrivals (2)' })).toBeVisible();
  await expect(arrivals).toContainText('metro-power-statement.pdf');
  await expect(arrivals).toContainText('IMG_0412-phone-scan.pdf');
  await page.goto('/');
  await expect(page.getByRole('list', { name: 'Documents' }).getByRole('link', { name: /Metro Power & Light/ })).toHaveCount(1);
  qb = await qbState(request);
  expect(qb.bills.filter((b) => b.DocNumber === 'MPL-2026-0415')).toHaveLength(1);
  expect(qb.create_bill_calls).toBe(1);
  await page.setViewportSize({ width: 1280, height: 900 });

  // --- Ask what happened, and verify against the saved history -------------
  await page.getByRole('button', { name: 'Ask about a bill' }).click();
  const dialog = page.getByRole('dialog', { name: 'Ask about a bill' });
  await dialog.getByLabel('Question').fill('What happened to the electricity bill?');
  await dialog.getByRole('button', { name: 'Ask' }).click();
  const answer = dialog.getByRole('region', { name: 'Answer' });
  await expect(answer).toContainText('Metro Power & Light');
  await expect(answer).toContainText('arrived 2 times');
  await expect(answer).toContainText('unpaid bill was created in QuickBooks');
  await expect(answer).toContainText('not a payment');
  const saved = await page.request.get(`/api/documents/${docA}/history`);
  const savedHistory: Array<{ summary: string; event_type: string }> = await saved.json();
  expect(savedHistory.filter((e) => e.event_type === 'arrival_recorded')).toHaveLength(2);
  const shown = dialog.getByRole('list', { name: 'Supporting history' }).getByRole('listitem');
  await expect(shown).toHaveCount(savedHistory.length);
  for (const [i, entry] of savedHistory.entries()) {
    await expect(shown.nth(i)).toContainText(entry.summary);
  }
  await expect(dialog.getByRole('link', { name: 'Original' })).toHaveAttribute('href', `/api/documents/${docA}/file`);
  await dialog.getByLabel('Question').fill('Which property was it for?');
  await dialog.getByRole('button', { name: 'Ask' }).click();
  await expect(answer).toContainText('is for Demo Property');
  await dialog.getByRole('button', { name: 'Close' }).click();

  // --- Fixture B: property unclear, corrected without posting --------------
  const notB = await uploadBill(page, 'city-water-notice.pdf', billB);
  await notB.getByRole('link', { name: 'Open document' }).click();
  await expect(page.getByRole('status', { name: 'Question for you' })).toContainText('Which property is this for?');
  await expect(page.getByRole('region', { name: 'Bill details' }).getByText('Unknown — needs review')).toHaveCount(1);
  await expect(page.getByRole('button', { name: 'Review and approve' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Edit details' }).click();
  await page.getByLabel('Property').selectOption({ label: 'Oak Street Duplex' });
  await page.getByRole('button', { name: 'Save correction' }).click();
  await expect(page.getByText('Correction saved. Nothing was sent to QuickBooks.')).toBeVisible();
  await expect(page.getByRole('region', { name: 'Bill details' })).toContainText('Oak Street Duplex');
  await expect(page.getByRole('region', { name: 'History' })).toContainText('Corrected property id: empty → Oak Street Duplex');
  await expect(page.getByRole('button', { name: 'Review and approve' })).toBeVisible();
  qb = await qbState(request);
  expect(qb.create_bill_calls).toBe(1); // saving did not post

  // --- Fixture C: bill already entered in QuickBooks -----------------------
  const billsBeforeC = qb.bills.length;
  const notC = await uploadBill(page, 'northside-electric-supply-inv.pdf', billC);
  await notC.getByRole('link', { name: 'Open document' }).click();
  await page.getByRole('button', { name: 'Check QuickBooks for existing bill' }).click();
  const match = page.getByRole('group', { name: 'Match NES-5531' });
  await expect(match).toContainText('Same vendor and invoice number NES-5531');
  await expect(page.getByRole('button', { name: 'Review and approve' })).toHaveCount(0);
  await match.getByRole('button', { name: 'Link to this bill' }).click();
  await expect(page.getByText(/Linked to existing bill NES-5531. No new bill was created./)).toBeVisible();
  await expect(page.getByText('Linked Existing Bill').first()).toBeVisible();
  qb = await qbState(request);
  expect(qb.bills.length).toBe(billsBeforeC);
  expect(qb.bills.filter((b) => b.DocNumber === 'NES-5531')).toHaveLength(1);
  expect(qb.create_bill_calls).toBe(1);

  // --- Signed-out access to the original is denied -------------------------
  await page.getByRole('button', { name: 'Sign out' }).click();
  const denied = await page.request.get(`/api/documents/${docA}/file`);
  expect(denied.status()).toBe(401);
});
