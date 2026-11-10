const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');

const root = path.resolve(__dirname, '..');
const pdfMake = require(path.join(root, 'miniapp/assets/pdfmake.min.js'));
pdfMake.addVirtualFileSystem(require(path.join(root, 'miniapp/assets/vfs_fonts.js')));

const html = fs.readFileSync(path.join(root, 'miniapp/index.html'), 'utf8');
const helpers = html.slice(html.indexOf('function formatDisplayDate('), html.indexOf('function downloadPurchasePdf('));
const builder = html.slice(html.indexOf('async function buildPurchasePdf()'), html.indexOf('let preparedPurchasePdf = null;'));
assert(helpers.includes('function localDateParts'));
assert(builder.includes('return new File'));

let documentDefinition;
const wrappedPdf = {
  createPdf(doc) {
    documentDefinition = doc;
    return pdfMake.createPdf(doc);
  },
};
const context = {
  state: {
    purchase: [
      { product: { name: 'Тестовый товар', stock: 1, price: 120, buy_price: 80 }, quantity: 2, purchasePrice: 80 },
      { product: { name: 'Заказанный товар', stock: 0, price: null, buy_price: null }, quantity: 3,
        purchasePrice: null, orderContributions: { order1: 3 } },
    ],
    purchaseBudget: 1000,
  },
  savedAddedOrderIds: new Set(['order1']),
  fetchCustomerOrdersData: async () => [{ id: 'order1', product_name: 'Заказанный товар', quantity: 3,
    customer_name: 'Тест', contact: '', note: '', has_photo: false }],
  loadPdfLibrary: async () => wrappedPdf,
  orderPhotoForPdf: async () => null,
  pdfText: value => String(value || ''),
  positionWord: count => count === 1 ? 'позиция' : 'позиции',
  File, setTimeout, clearTimeout, console, Date, Number, String, Set, Object, Promise,
};

vm.createContext(context);
vm.runInContext(helpers + builder + '; globalThis.buildPurchasePdf = buildPurchasePdf;', context);
context.buildPurchasePdf().then(async file => {
  const bytes = Buffer.from(await file.arrayBuffer());
  assert.strictEqual(bytes.subarray(0, 4).toString(), '%PDF');
  assert(file.name.endsWith('.pdf'));

  const purchaseTable = documentDefinition.content.find(item => item.table?.widths?.length === 4);
  assert.strictEqual(purchaseTable.table.body.length, 2);
  assert.strictEqual(purchaseTable.table.body[1][0].text, 'Тестовый товар');
  assert.strictEqual(purchaseTable.table.body[1][1].text, '2');
  assert(documentDefinition.content.some(item => item.text?.startsWith('Итого по указанным закупочным ценам: 160')));

  const ordersTable = documentDefinition.content.find(item => item.table?.widths?.length === 1);
  assert.strictEqual(ordersTable.table.body.length, 2);
  assert(JSON.stringify(ordersTable.table.body[1]).includes('Заказанный товар'));
  console.log('purchase_pdf_valid', bytes.length, 'bytes; customer order excluded from purchase total');
}).catch(error => { console.error(error); process.exitCode = 1; });
