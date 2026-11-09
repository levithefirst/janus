// Regression: the first tap on "Approve PayPal hold" must reach PayPal even when the live refresh (which polls
// every 4 s) sees the new state before the redirect arrives. Usage: node first_tap.js <customer-link>
// Prints the final state chip text. "authorized" means the tap went through the (mock) PayPal approval and back.
const { chromium, devices } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ ...devices['iPhone 13'] })).newPage();
  await page.goto(process.argv[2]);
  await page.click('text=Approve PayPal hold');
  await page.waitForTimeout(Number(process.argv[3] || 9000));
  console.log('STATE=' + (await page.locator('.state').first().textContent()).trim());
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
