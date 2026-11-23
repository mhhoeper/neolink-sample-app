const test = require('node:test');
const assert = require('node:assert/strict');
const T = require('../app/static/timeline.js');
const day = '2026-10-25';
const row = { id: 'a', start: day + 'T02:30:00', end: day + 'T02:30:30', duration: 30 };

test('camera times remain wall-clock times across DST and midnight', () => {
  assert.equal(T.seconds(row.start, day), 9000);
  assert.equal(T.seconds('2026-10-26T00:00:10', day), 86410);
  assert.equal(T.seconds('2026-10-24T23:59:50', day), -10);
  assert.equal(T.dayNumber('2026-02-30'), null);
  assert.equal(T.seconds(day + 'T24:00:00', day), null);
  assert.equal(T.bounds({start: null}, day), null);
});
test('cross-midnight and incomplete recordings stay selectable', () => {
  const crossing = {start: day + 'T23:59:50', end: '2026-10-26T00:00:20'};
  assert.deepEqual(T.bounds(crossing, day), {start:86390,end:86420});
  assert.equal(T.intersects(crossing, '2026-10-26', 0, 600), true);
  assert.deepEqual(T.bounds({start:row.start,duration:30}, day), {start:9000,end:9030});
  assert.equal(T.intersects(row, day, 9030, 9100), false);
});
test('seek and cursor refer to the same recording section', () => {
  assert.equal(T.seekOffset(row, day, 9012), 12);
  assert.equal(T.seekOffset(row, day, 8990), 0);
  assert.equal(T.seekOffset(row, day, 9990), 30);
  assert.equal(T.mediaOffset(30, 20), 19.99);
  assert.equal(T.cursor(row, day, 12), 9012);
  assert.equal(T.cursor(row, day, 40), 9030);
  assert.equal(T.cursor({start:null},day,3), null);
  assert.equal(T.clock(86400), '24:00');
  assert.equal(T.clock(-10,true), '23:59:50');
});
test('crowded hit targets group recordings without dropping their identity', () => {
  const overlap = {...row,id:'b',start:day+'T02:30:15',end:day+'T02:30:45'};
  const far = {...row,id:'c',start:day+'T03:00:00',end:day+'T03:00:20'};
  const unknown = {id:'unknown',start:null};
  const groups = T.groups([far,overlap,row,unknown],day,7200,14400,720);
  assert.deepEqual(groups.map(group=>group.events.map(item=>item.id)), [['a','b'],['c']]);
  assert.deepEqual([groups[0].from,groups[0].to],[9000,9045]);
  assert.deepEqual(T.groups([row],day,0,86400,0),[]);
});

test('rolling window supports yesterday, midnight, dated labels and zoom bounds', () => {
  const day = '2026-11-23', lower = -43200, upper = 43200;
  assert.equal(T.timestamp(day, lower), '2026-11-22T12:00:00');
  assert.equal(T.timestamp(day, upper), '2026-11-23T12:00:00');
  assert.equal(T.dateClock(day, lower), '2026-11-22 12:00');
  assert.equal(T.dateClock(day, 86400), '2026-11-24 00:00');
  assert.equal(T.clampWindow(86400, 0, lower, upper), lower);
  assert.equal(T.clampWindow(600, -43200, lower, upper), lower);
  assert.equal(T.clampWindow(600, 43200, lower, upper), upper-600);
  const row = {start:'2026-11-22T23:59:50',end:'2026-11-23T00:00:20'};
  assert.equal(T.seekOffset(row,day,5),15);
  assert.equal(T.cursor(row,day,15),5);
  assert.equal(T.intersects(row,day,lower,upper),true);
});
