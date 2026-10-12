import { guidedTour } from '../../views/tour';

const userIndexes = { first: 0, second: 1 };
const loggingCredentials = (index: string) => {
  const suffix = userIndexes[index] === 0 ? '' : '1';
  const username = Cypress.env(`LOGIN_USERNAME${suffix}`);
  const password = Cypress.env(`LOGIN_PASSWORD${suffix}`);
  if (!(index in userIndexes) || !username || !password) {
    throw new Error(`Configure CYPRESS_LOGIN_USERS with credentials for the ${index} logging user`);
  }
  return { username, password };
};
export const loggingUsername = (index: string): string => loggingCredentials(index).username;

declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace Cypress {
    interface Chainable {
      uiLoginAsUser(index: string): Chainable<void>;
      uiLoginAsClusterAdminForUser(index: string): Chainable<void>;
      uiLogoutUser(index: string): Chainable<void>;
      uiLogoutClusterAdminForUser(index: string): Chainable<void>;
      uiImpersonateUser(index: string): Chainable<void>;
      switchToDevConsole(): Chainable<void>;
      switchToAdmConsole(): Chainable<void>;
    }
  }
}
Cypress.Commands.add('uiLoginAsUser', (index: string) => {
  const { username, password } = loggingCredentials(index);
  const oauthUrl =
    Cypress.env('OAUTH_URL') ||
    Cypress.config('baseUrl').replace('console-openshift-console', 'oauth-openshift');
  cy.login(Cypress.env('LOGIN_IDP'), username, password, oauthUrl);
  cy.visit('/');
  guidedTour.close();
});
// The first configured account must already be a cluster admin.
Cypress.Commands.add('uiLoginAsClusterAdminForUser', (index: string) => {
  cy.uiLoginAsUser(index);
  cy.switchToAdmConsole();
});
Cypress.Commands.add('uiLogoutUser', () => {
  cy.uiLogout();
});
Cypress.Commands.add('uiLogoutClusterAdminForUser', () => {
  cy.uiLogout();
});
Cypress.Commands.add('switchToDevConsole', () => {
  cy.switchPerspective('Developer');
  guidedTour.close();
});
Cypress.Commands.add('switchToAdmConsole', () => {
  cy.switchPerspective('Administrator', 'Core platform');
  guidedTour.close();
});
Cypress.Commands.add('uiImpersonateUser', (index: string) => {
  const username = loggingUsername(index);
  cy.switchToAdmConsole();
  cy.clickNavLink(['User Management', 'Users']);
  cy.contains('td', `${Cypress.env('LOGIN_IDP')}:${username}`)
    .closest('tr')
    .find('button[data-test-id="kebab-button"]')
    .click();
  cy.contains('button', 'Impersonate User').click();
  cy.byTestID('username').should('contain', username);
  guidedTour.close();
});
