select g as id, md5(g::text) as hash, g * 1.5 as valor, now() as criado, (g % 7) as grupo from generate_series(1, 10000) g;
