with subtab_nac:
        st.markdown("Administra los agentes nacionales autorizados para inyectar/retirar energía (código iniciado con **3**).")
        c_cat_nac = obtener_conexion()
        try:
            asegurar_esquema(c_cat_nac)
            df_nac_cat = ejecutar_consulta_df(
                "SELECT codigo_agente, nombre_agente, COALESCE(activo, 1) AS activo FROM dim_agentes_nacionales ORDER BY codigo_agente",
                c_cat_nac,
            )
        finally:
            c_cat_nac.close()

        if df_nac_cat.empty:
            df_nac_cat = pd.DataFrame(columns=["codigo_agente", "nombre_agente", "activo"])
        else:
            df_nac_cat["codigo_agente"] = df_nac_cat["codigo_agente"].apply(lambda x: limpiar_codigo(x).upper())
            df_nac_cat["nombre_agente"] = df_nac_cat["nombre_agente"].apply(limpiar_texto_catalogo)
            df_nac_cat["activo"] = df_nac_cat["activo"].apply(lambda x: bool(int(x)) if pd.notna(x) else True)

        if es_admin:
            edited_nac = st.data_editor(
                df_nac_cat,
                use_container_width=True,
                hide_index=True,
                num_rows="dynamic",
                column_config={
                    "codigo_agente": st.column_config.TextColumn("Código Agente Nacional (Ej. 3DENEE)", required=True),
                    "nombre_agente": st.column_config.TextColumn("Nombre / Razón Social", width="large"),
                    "activo": st.column_config.CheckboxColumn("Activo", default=True),
                },
                key="editor_agentes_nacionales",
            )

            if st.button("💾 Guardar Agentes Nacionales", type="primary", key="btn_save_nac"):
                c_save_nac = obtener_conexion()
                asegurar_esquema(c_save_nac)
                cur = c_save_nac.cursor()
                codigos_en_tabla = []
                invalidos = []
                for _, row in edited_nac.iterrows():
                    cod = limpiar_codigo(row.get("codigo_agente")).upper()
                    if not cod:
                        continue
                    if not cod.startswith("3"):
                        invalidos.append(cod)
                        continue
                    codigos_en_tabla.append(cod)
                    nom = limpiar_texto_catalogo(row.get("nombre_agente"))
                    act_val = 1 if bool(row.get("activo", True)) else 0
                    ejecutar_sql(
                        cur,
                        """
                        INSERT INTO dim_agentes_nacionales (codigo_agente, nombre_agente, activo)
                        VALUES (?, ?, ?)
                        ON CONFLICT (codigo_agente) DO UPDATE SET
                            nombre_agente = excluded.nombre_agente,
                            activo = excluded.activo
                        """,
                        (cod, nom, act_val),
                    )
                if codigos_en_tabla:
                    placeholders = ", ".join(["?"] * len(codigos_en_tabla))
                    ejecutar_sql(
                        cur,
                        f"DELETE FROM dim_agentes_nacionales WHERE codigo_agente NOT IN ({placeholders})",
                        tuple(codigos_en_tabla),
                    )
                else:
                    ejecutar_sql(cur, "DELETE FROM dim_agentes_nacionales")
                c_save_nac.commit()
                cur.close()
                c_save_nac.close()
                st.cache_data.clear()
                if invalidos:
                    st.warning(f"Se omitieron códigos que no inician con '3': {', '.join(invalidos)}")
                st.success("✅ Tabla de agentes nacionales actualizada.")
                st.rerun()
        else:
            st.dataframe(df_nac_cat, use_container_width=True, hide_index=True)

    with subtab_cf:
        st.markdown("Asigna cada **Código Contrato Firme** (ej. `CF13M0000704`) a su agente nacional titular para evitar declaraciones cruzadas.")
        c_cat_cf = obtener_conexion()
        try:
            asegurar_esquema(c_cat_cf)
            df_cf_cat = ejecutar_consulta_df(
                """
                SELECT codigo_contrato_firme, agente_autorizado,
                       COALESCE(agente_contraparte, '') AS agente_contraparte,
                       COALESCE(descripcion, '') AS descripcion,
                       COALESCE(activo, 1) AS activo
                  FROM dim_contratos_firmes
                 ORDER BY codigo_contrato_firme
                """,
                c_cat_cf,
            )
        finally:
            c_cat_cf.close()

        if df_cf_cat.empty:
            df_cf_cat = pd.DataFrame(columns=["codigo_contrato_firme", "agente_autorizado", "agente_contraparte", "descripcion", "activo"])
        else:
            df_cf_cat["codigo_contrato_firme"] = df_cf_cat["codigo_contrato_firme"].apply(lambda x: limpiar_codigo(x).upper())
            df_cf_cat["agente_autorizado"] = df_cf_cat["agente_autorizado"].apply(lambda x: limpiar_codigo(x).upper())
            df_cf_cat["agente_contraparte"] = df_cf_cat["agente_contraparte"].apply(lambda x: limpiar_codigo(x).upper())
            df_cf_cat["descripcion"] = df_cf_cat["descripcion"].apply(limpiar_texto_catalogo)
            df_cf_cat["activo"] = df_cf_cat["activo"].apply(lambda x: bool(int(x)) if pd.notna(x) else True)

        if es_admin:
            edited_cf = st.data_editor(
                df_cf_cat,
                use_container_width=True,
                hide_index=True,
                num_rows="dynamic",
                column_config={
                    "codigo_contrato_firme": st.column_config.TextColumn("Código Contrato Firme", required=True),
                    "agente_autorizado": st.column_config.TextColumn("Agente Titular (Ej. 3DENEE)", required=True),
                    "agente_contraparte": st.column_config.TextColumn("Contraparte (Opcional)"),
                    "descripcion": st.column_config.TextColumn("Descripción", width="large"),
                    "activo": st.column_config.CheckboxColumn("Activo", default=True),
                },
                key="editor_contratos_firmes",
            )

            if st.button("💾 Guardar Contratos Firmes", type="primary", key="btn_save_cf"):
                c_save_cf = obtener_conexion()
                asegurar_esquema(c_save_cf)
                cur = c_save_cf.cursor()
                cods_cf = []
                for _, row in edited_cf.iterrows():
                    cod_cf = limpiar_codigo(row.get("codigo_contrato_firme")).upper()
                    ag_tit = limpiar_codigo(row.get("agente_autorizado")).upper()
                    if not cod_cf or not ag_tit:
                        continue
                    cods_cf.append(cod_cf)
                    ag_cp = limpiar_codigo(row.get("agente_contraparte")).upper()
                    desc = limpiar_texto_catalogo(row.get("descripcion"))
                    act_val = 1 if bool(row.get("activo", True)) else 0
                    ejecutar_sql(
                        cur,
                        """
                        INSERT INTO dim_contratos_firmes (codigo_contrato_firme, agente_autorizado, agente_contraparte, descripcion, activo)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT (codigo_contrato_firme) DO UPDATE SET
                            agente_autorizado = excluded.agente_autorizado,
                            agente_contraparte = excluded.agente_contraparte,
                            descripcion = excluded.descripcion,
                            activo = excluded.activo
                        """,
                        (cod_cf, ag_tit, ag_cp, desc, act_val),
                    )
                if cods_cf:
                    placeholders_cf = ", ".join(["?"] * len(cods_cf))
                    ejecutar_sql(
                        cur,
                        f"DELETE FROM dim_contratos_firmes WHERE codigo_contrato_firme NOT IN ({placeholders_cf})",
                        tuple(cods_cf),
                    )
                else:
                    ejecutar_sql(cur, "DELETE FROM dim_contratos_firmes")
                c_save_cf.commit()
                cur.close()
                c_save_cf.close()
                st.cache_data.clear()
                st.success("✅ Catálogo de contratos firmes actualizado.")
                st.rerun()
        else:
            st.dataframe(df_cf_cat, use_container_width=True, hide_index=True)
