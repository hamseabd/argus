'use strict';

const rows = [];

async function openDb() {
  let open = true;
  return {
    async insert(table, record) {
      if (!open) throw new Error('connection closed');
      await new Promise((resolve) => setTimeout(resolve, 1));
      rows.push({ table, record });
    },
    async close() {
      open = false;
    },
  };
}

module.exports = { openDb, rows };
