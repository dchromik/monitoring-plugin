import { TestIds } from '@/features/logging/test-ids';
import { aggrLogTest, commonTest, isDevConsoleReady } from './test-utils';
import { APP_NAMESPACE1, APP_NAMESPACE2 } from './test-utils';

let SKIPALL = false;

function devConsoleUserAggrTest() {
  it('user can not display infra logs', { tags: ['@logging-aggr'] }, () => {
    cy.runLogQuery('{{} k8s_namespace_name="openshift-monitoring" {}}');
    cy.byTestID(TestIds.LogsTable).within(() => {
      cy.get('.lv-plugin__table__row-error').should('exist');
      // It may  report error below
      //-' DateMessageDanger alert:{"error":"400 Bad Request","errorType":"observatorium-api","statu
      //+'Warning alert:No datapoints found'
    });
  });
}

describe(
  'DevConsole: Admin in AggregatedLogs ',
  { tags: ['@logging', '@logging-admin', '@coo'] },
  () => {
    before(function () {
      //Check if DevConsole is ready
      isDevConsoleReady().then((ready) => {
        if (!ready) {
          SKIPALL = true;
          cy.task('log', 'DeveloperConsole is not ready — skipping suite');
          this.skip();
        }
      });
      cy.uiLoginAsClusterAdminForUser('first');
      cy.switchToDevConsole();
    });

    beforeEach(function () {
      cy.showDevConsolePodAggrLog(APP_NAMESPACE1);
    });

    after(function () {
      if (!SKIPALL) {
        cy.uiLogoutClusterAdminForUser('first');
      }
    });
    aggrLogTest();
    commonTest();

    it('admin can display infra container logs', { tags: ['@logging-aggr'] }, () => {
      //load Aggregated Logs for pod in openshift-monitoring
      cy.showDevConsolePodAggrLog('openshift-monitoring');
      cy.assertInfraLogsInLogsTable();
    });
  },
);

describe(
  'DevConsole: Impersonate User in AggregatedLogs',
  { tags: ['@logging', '@logging-admin', '@coo'] },
  () => {
    before(function () {
      //Check if DevConsole is ready
      isDevConsoleReady().then((ready) => {
        if (!ready) {
          cy.task('log', 'DeveloperConsole is not ready — skipping suite');
          SKIPALL = true;
          this.skip();
        }
      });
      cy.grantLogViewRolesToUser('second', `${APP_NAMESPACE1}`);
      cy.grantLogViewRolesToUser('second', `${APP_NAMESPACE2}`);
      cy.uiLoginAsClusterAdminForUser('first');
      cy.uiImpersonateUser('second');
      cy.switchToDevConsole();
    });

    beforeEach(function () {
      cy.showDevConsolePodAggrLog(APP_NAMESPACE1);
    });

    after(function () {
      if (!SKIPALL) {
        cy.uiLogoutUser('second');
        cy.removeLogViewRolesFromUser('second', `${APP_NAMESPACE1}`);
        cy.removeLogViewRolesFromUser('second', `${APP_NAMESPACE2}`);
      }
    });
    // devConsoleUserAggrTest(); skip for bug  https://github.com/openshift/logging-view-plugin/pull
    aggrLogTest();
    commonTest();
  },
);

describe(
  'DevConsole: User in AggregatedLogs',
  { tags: ['@logging', '@logging-admin', '@coo'] },
  () => {
    before(function () {
      //Check if DevConsole is ready
      isDevConsoleReady().then((ready) => {
        if (!ready) {
          cy.task('log', 'DeveloperConsole is not ready — skipping suite');
          SKIPALL = true;
          this.skip();
        }
      });
      cy.grantLogViewRolesToUser('second', `${APP_NAMESPACE1}`);
      cy.grantLogViewRolesToUser('second', `${APP_NAMESPACE2}`);
      cy.uiLoginAsUser('second');
      cy.switchToDevConsole();
    });

    beforeEach(function () {
      //hover on project page
      cy.showDevConsolePodAggrLog(APP_NAMESPACE1);
    });

    after(function () {
      if (!SKIPALL) {
        cy.uiLogoutUser('second');
        cy.removeLogViewRolesFromUser('second', `${APP_NAMESPACE1}`);
        cy.removeLogViewRolesFromUser('second', `${APP_NAMESPACE2}`);
      }
    });
    devConsoleUserAggrTest();
    aggrLogTest();
    commonTest();
  },
);
