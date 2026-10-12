/* eslint-disable @typescript-eslint/no-namespace */
/// <reference types="cypress" />
import 'cypress-wait-until';
import { TestIds } from '@/features/logging/test-ids';
import { loggingUsername } from './logging-console-auth';

declare global {
  namespace Cypress {
    interface IndexField {
      name: string;
      value?: string;
    }
    interface Chainable {
      grantLogViewRolesToUser(index: string, project: string);
      removeLogViewRolesFromUser(index: string, project: string);
      showLogQueryInput();
      closeLogQueryInput();
      setLogQueryInput(query: string);
      runLogQuery(logQL: string);
      assertLogsInLogsTable();
      assertFieldsInLogDetail(indexFields: Cypress.IndexField[]): Chainable<void>;
      assertAppLogsInLogsTable();
      assertInfraLogsInLogsTable();
      assertAuditLogsInLogsTable();
      selectLogTenant(tenant: string);
      selectLogAttribute(attribute: string);
      checkLogNamespaces(namespaces: string[]);
      checkLogPods(pods: string[]);
      checkLogContainers(containers: string[]);
      showAdminConsolePodAggrLog(projectName: string, podName?: string);
      showDevConsolePodAggrLog(projectName: string, podName?: string);
      checkLogDropboxOptions(opts: string[]);
      clickPodLink(podName?: string);
    }
  }
}

Cypress.Commands.add('showLogQueryInput', () => {
  cy.get(`[data-test="${TestIds.ShowQueryToggle}"]`).then(($btn) => {
    if ($btn.text().includes('Show Query')) {
      cy.wrap($btn).click({ force: true });
    }
  });
});

Cypress.Commands.add('closeLogQueryInput', () => {
  cy.get(`[data-test="${TestIds.ShowQueryToggle}"]`).then(($btn) => {
    if ($btn.text().includes('Hide Query')) {
      cy.wrap($btn).click({ force: true });
    }
  });
});

// Reliably set the LogQL query input in a single React onChange.
Cypress.Commands.add('setLogQueryInput', (query: string) => {
  cy.byTestID(TestIds.LogsQueryInput)
    .find('textarea')
    .then(($textarea) => {
      const textarea = $textarea[0] as HTMLTextAreaElement;
      const win = textarea.ownerDocument.defaultView ?? window;
      const nativeValueSetter = Object.getOwnPropertyDescriptor(
        win.HTMLTextAreaElement.prototype,
        'value',
      )?.set;
      nativeValueSetter?.call(textarea, query);
      textarea.dispatchEvent(new win.Event('input', { bubbles: true }));
    });
  cy.byTestID(TestIds.LogsQueryInput).find('textarea').should('have.value', query);
  cy.location('search').should((search) => {
    expect(new URLSearchParams(search).get('q')).to.eq(query.trim());
  });
  cy.byTestID(TestIds.LogsQueryInput).should('have.attr', 'data-test-query', query.trim());
});

Cypress.Commands.add('runLogQuery', (logQL: string) => {
  cy.showLogQueryInput();
  cy.byTestID(TestIds.LogsQueryInput)
    .find('textarea')
    .type(`{selectall}${logQL}`, { delay: 0 })
    .then(() => {
      cy.byTestID(TestIds.ExecuteQueryButton).click();
    });
});

// Test users must be dedicated accounts; grants are removed after each suite.
for (const [command, action] of [
  ['grantLogViewRolesToUser', 'add-role-to-user'],
  ['removeLogViewRolesFromUser', 'remove-role-from-user'],
] as const) {
  Cypress.Commands.add(command, (index: string, project: string) => {
    const username = loggingUsername(index);
    for (const role of [
      'view',
      'cluster-logging-application-view',
      'monitoring-rules-edit',
      'cluster-monitoring-view',
    ]) {
      cy.adminCLI(`oc -n ${project} policy ${action} ${role} ${username}`, {
        failOnNonZeroExit: action === 'add-role-to-user',
      });
    }
  });
}

Cypress.Commands.add('assertLogsInLogsTable', () => {
  // Ensure the table has loaded rows
  cy.byTestID(TestIds.LogsTable).within(() => {
    cy.get('tr[data-test-rows="resource-row" ]')
      .first()
      .within(() => {
        cy.get('td[data-label="date"]').should('exist');
        cy.get('td[data-label="message"]').should('exist');
      });
    //there are more than one row
    cy.get('tr[data-test-rows="resource-row" ]').its('length').should('be.gt', 1);
  });
});

//Expand the the first row and check the expected fields in the log detail
//the filed value can be value, empty or pattern
Cypress.Commands.add('assertFieldsInLogDetail', (indexFields: Cypress.IndexField[]) => {
  cy.byTestID(TestIds.LogsTable).within(() => {
    //there are more than one row in tables
    cy.get('tr[data-test-rows="resource-row"]').its('length').should('be.gt', 1);

    //show the detail table
    cy.get('tr[data-test-rows="resource-row"]')
      .first()
      .find('td')
      .first()
      .find('button')
      .then(($btn) => {
        if ($btn.attr('aria-expanded') !== 'true') {
          cy.wrap($btn).click({ force: true });
        }
      });

    // check the fields in detail
    cy.get('.lv-plugin__table__details').within(() => {
      indexFields.forEach((field) => {
        if (!Cypress._.isEmpty(field.value)) {
          // skip if value is empty, null, or undefined
          const pattern = new RegExp(field.value);
          cy.contains('dt', field.name)
            .parent()
            .within(() => {
              cy.get('dd').invoke('text').should('match', pattern);
            });
        } else {
          cy.contains('dt', field.name);
        }
      });
    });
  });
});

Cypress.Commands.add('assertAppLogsInLogsTable', () => {
  //only check some of these index fileds
  const indexFields: Cypress.IndexField[] = [
    { name: 'openshift_log_type', value: 'application' },
    { name: 'k8s_pod_name', value: '' },
    { name: 'k8s_namespace_name', value: '' },
    { name: 'k8s_node_name', value: '' },
  ];
  cy.assertFieldsInLogDetail(indexFields);
});

Cypress.Commands.add('assertInfraLogsInLogsTable', () => {
  //only check some of these index fileds
  const indexFields: Cypress.IndexField[] = [
    { name: 'openshift_log_type', value: 'infrastructure' },
  ];
  cy.assertFieldsInLogDetail(indexFields);
});

Cypress.Commands.add('assertAuditLogsInLogsTable', () => {
  //only check some of these index fileds
  const indexFields: Cypress.IndexField[] = [{ name: 'openshift_log_type', value: 'audit' }];
  cy.assertFieldsInLogDetail(indexFields);
});

//select a tenant for logs
Cypress.Commands.add('selectLogTenant', (tenant: string) => {
  cy.byTestID(TestIds.TenantToggle).click();
  cy.get('#logging-view-tenant-dropdown').contains('button', tenant).click();
  //close the dropdown list
  cy.get('body').click(0, 0);
});

//select one Attribute in AttributeFilters
Cypress.Commands.add('selectLogAttribute', (attribute: string) => {
  cy.byTestID(TestIds.AvailableAttributes).click();
  cy.byTestID(TestIds.AttributeFilters).contains('button', attribute).click();
  //close the dropdown list
  cy.get('body').click(0, 0);
});

//select options from AttributeOptions dropbox
Cypress.Commands.add('checkLogDropboxOptions', (opts: string[]) => {
  opts.forEach((opt) => {
    cy.byTestID(TestIds.AttributeOptions)
      .find('input[type="text"]') // input the expected options
      .click()
      .clear()
      .type(opt);
    cy.byTestID(TestIds.AttributeFilters, { timeout: 20000 }).within(() => {
      cy.contains('[role="listbox"] li', opt, { timeout: 20000, includeShadowDom: true })
        .scrollIntoView() // in case it's offscreen
        .find('input[type="checkbox"]')
        .check({ force: true }) // safe: never unchecks if already checked
        .should('be.checked');
    });
  });
  //close the dropdown panel
  cy.get('body').click(0, 0);
});

//select namespaces from AttributeOptions
Cypress.Commands.add('checkLogNamespaces', (namespaces: string[]) => {
  cy.selectLogAttribute('Namespaces');
  cy.checkLogDropboxOptions(namespaces);
});

//select pods from AttributeOptions
Cypress.Commands.add('checkLogPods', (pods: string[]) => {
  cy.selectLogAttribute('Pods');
  cy.checkLogDropboxOptions(pods);
});

//select Containers from AttributeOptions
Cypress.Commands.add('checkLogContainers', (containers: string[]) => {
  cy.selectLogAttribute('Containers');
  cy.checkLogDropboxOptions(containers);
});

//if no podName, will click the first pod
Cypress.Commands.add('clickPodLink', (podName?: string) => {
  const name = podName ?? '.*';
  const pattern = new RegExp(`/k8s/ns/.*/pods/${name}`);
  cy.get('td a')
    .filter((_, a) => pattern.test((a as HTMLAnchorElement).href))
    .first() // pick the first match only
    .click();
});

//Show the AdminConsole pod logs in one project
Cypress.Commands.add('showAdminConsolePodAggrLog', (projectName: string, podName?: string) => {
  //navigate to  Workloads->Pods
  cy.clickNavLink(['Workloads', 'Pods']);
  //select namespace
  cy.changeNamespace(projectName);
  //select the first pod matching the podName
  if (!podName) {
    cy.clickPodLink();
  } else {
    cy.clickPodLink(podName);
  }
  cy.byLegacyTestID('horizontal-link-Aggregated Logs').click();
  cy.byTestID(TestIds.LogsTable, { timeout: 300000 }).should('exist');
});

//Show the devConsole pod logs in one project
Cypress.Commands.add('showDevConsolePodAggrLog', (projectName: string, podName?: string) => {
  //navigate to Project page
  cy.clickNavLink(['Project']);
  //select namespace
  cy.changeNamespace(projectName);
  //navigate pods page in the project
  cy.get(`a[data-test="resource-inventory-item"][href="/k8s/ns/${projectName}/pods"]`)
    .scrollIntoView()
    .click({ force: true });
  //select the first pod matching the podName
  if (!podName) {
    cy.clickPodLink();
  } else {
    cy.clickPodLink(podName);
  }
  cy.byLegacyTestID('horizontal-link-Aggregated Logs').click();
  cy.byTestID(TestIds.LogsTable, { timeout: 300000 }).should('exist');
});
