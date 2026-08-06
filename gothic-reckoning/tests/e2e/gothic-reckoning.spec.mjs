import { test, expect } from '@playwright/test';

class GothicReckoningPage {
  constructor(page) {
    this.page = page;
    this.begin = page.getByRole('button', { name: 'Begin the Night' });
    this.continue = page.getByRole('button', { name: /Let Night Pass|Hear the Village|Cast Judgment|To Sleep/ });
    this.privacy = page.getByRole('link', { name: 'privacy' });
    this.seats = page.locator('.seat');
    this.record = page.locator('#log');
  }

  async goto() {
    await this.page.goto('/');
    await expect(this.page.getByRole('heading', { name: 'Gothic Reckoning' })).toBeVisible();
  }

  async beginTale() {
    await this.begin.click();
    await expect(this.seats).toHaveCount(12);
    await expect(this.page.getByText(/12 souls remain/)).toBeVisible();
  }

  async passNight() {
    const response = this.page.waitForResponse((r) => r.url().endsWith('/api/game/advance') && r.status() === 200);
    await this.page.getByRole('button', { name: 'Let Night Pass' }).click();
    await response;
    await expect(this.page.getByText(/Day 1/)).toBeVisible();
  }
}

test.describe('Gothic Reckoning: core player journey', () => {
  test('opens the 90s gothic title screen and begins a playable tale', async ({ page }) => {
    const gothic = new GothicReckoningPage(page);
    await gothic.goto();
    await expect(page.getByText('a werewolf tale in twelve souls')).toBeVisible();
    await gothic.beginTale();
    await expect(gothic.record).toContainText('The village gathers');
    await page.screenshot({ path: 'artifacts/gothic-table.png', fullPage: true });
  });

  test('resolves a night into dawn and records the victim', async ({ page }) => {
    const gothic = new GothicReckoningPage(page);
    await gothic.goto();
    await gothic.beginTale();
    await gothic.passNight();
    await expect(gothic.record).toContainText(/was found at dawn/);
    await expect(page.locator('.seat.dead')).toHaveCount(1);
  });

  test('links to its in-app privacy policy', async ({ page }) => {
    const gothic = new GothicReckoningPage(page);
    await gothic.goto();
    await gothic.privacy.click();
    await expect(page).toHaveURL(/privacy\.html$/);
    await expect(page.getByRole('heading', { name: 'Privacy at the Reckoning' })).toBeVisible();
    await expect(page.getByText(/does not collect, sell, share/)).toBeVisible();
  });
});
