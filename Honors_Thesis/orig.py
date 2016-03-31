import numpy as np
from time import clock
from os import makedirs
import math
from dolfin import ERROR, set_log_level, exp, near, File, Expression, tanh, \
    Constant, SubDomain, VectorFunctionSpace, FunctionSpace, \
    DirichletBC, Function, split, dx,\
    MixedFunctionSpace, TestFunctions, inner, sym, grad, div,\
    RectangleMesh, Point, solve, project, assign, interpolate

'''
This version of the code runs a swarm of simulations of various viscosities
and temperatures per viscosity. Since 5-29, it also includes an adiabatic
temperature variance at the LAB.
'''

set_log_level(ERROR)

rho_0 = 3300.
rhomelt = 2900.
darcy = 1e-13  # k over (mu*phi) in darcy
alpha = 2.5e-5
g = 9.81
# not actually used.. (in cian's notes he is using the non dimensional
# kappa in the equations, which here i have defined as 1 (as
# kappa/kappa_0) so i think i can get away with this)
kappa = 1.E-6

b = 12.7
cc = math.log(128)
#Ep  = 0.014057
theta = 0.5
h = 1000000.0
kappa_0 = 1.E-6

output_every = 100
nx = 20
ny = 20
# non-dimensional mesh size
MeshHeight = 0.4
MeshWidth = 1.0
LABHeight = 0.75 * MeshHeight


class LithosExp(Expression):

    def eval(self, values, x):
        height = 0.05
        width = 0.2
        scale = 0.05

        def ridge(offset):
            height * (1 - tanh((x[0] - (0.5 + offset) * MeshWidth) / scale))

        hump = ridge(width) - ridge(-width)
        values[0] = LABHeight - hump

LAB = LithosExp()


def top(x, on_boundary): return on_boundary and near(x[1], MeshHeight)


def bottom(x, on_boundary): return on_boundary and near(x[1], 0.0)


def left(x, on_boundary): return on_boundary and near(x[0], 0.0)


def right(x, on_boundary): return on_boundary and near(x[0], MeshWidth)


def RunJob(Tb, mu_value, path):
    runtimeInit = clock()

    tfile = File(path + '/t6t.pvd')
    mufile = File(path + "/mu.pvd")
    ufile = File(path + '/velocity.pvd')
    gradpfile = File(path + '/gradp.pvd')
    pfile = File(path + '/pstar.pvd')
    parameters = open(path + '/parameters', 'w', 0)
    vmeltfile = File(path + '/vmelt.pvd')
    rhofile = File(path + '/rhosolid.pvd')
    fluidTemp = File(path + '/fluid_temp.pvd')

    for name in dir():
        ev = str(eval(name))
        if name[0] != '_' and ev[0] != '<':
            parameters.write(name + ' = ' + ev + '\n')

    temp_values = [27. + 273, Tb + 273, 1300. + 273, 1305. + 273]
    dTemp = temp_values[3] - temp_values[0]
    temp_values = [x / dTemp for x in temp_values]  # non dimensionalising temp

    mu_a = mu_value  # this was taken from the blankenbach paper, can change

    Ep = b / dTemp

    mu_bot = exp(-Ep * (temp_values[3] * dTemp - 1573) + cc) * mu_a

    Ra = rho_0 * alpha * g * dTemp * h**3 / (kappa_0 * mu_a)
    w0 = rho_0 * alpha * g * dTemp * h**2 / mu_a
    tau = h / w0
    p0 = mu_a * w0 / h

    print(mu_a, mu_bot, Ra, w0, p0)

    vslipx = 1.6e-09 / w0
    vslip = Constant((vslipx, 0.0))  # nondimensional
    noslip = Constant((0.0, 0.0))

    dt = 3.E11 / tau
    tEnd = 3.E15 / tau  # non-dimensionalising times

    class PeriodicBoundary(SubDomain):

        def inside(self, x, on_boundary):
            return left(x, on_boundary)

        def map(self, x, y):
            y[0] = x[0] - MeshWidth
            y[1] = x[1]

    pbc = PeriodicBoundary()

    class TempExp(Expression):

        def eval(self, value, x):
            if x[1] >= LAB(x):
                value[0] = temp_values[0] + \
                    (temp_values[1] - temp_values[0]) * \
                    (MeshHeight - x[1]) / (MeshHeight - LAB(x))
            else:
                value[0] = temp_values[3] - \
                    (temp_values[3] - temp_values[2]) * (x[1]) / (LAB(x))

    class FluidTemp(Expression):

        def __init__(self, P):
            self.T0 = T0

        def eval(self, value, x):
            t_val = np.zeros(1, dtype='d')
            self.T0.eval(t_val, x)

            LAB_temp = 1.013
            # value[0] = min(
            if t_val <= LAB_temp:
                value[0] = LAB_temp
            else:
                value[0] = t_val

    mesh = RectangleMesh(Point(0.0, 0.0), Point(MeshWidth, MeshHeight), nx, ny)

    Svel = VectorFunctionSpace(mesh, 'CG', 2, constrained_domain=pbc)
    Spre = FunctionSpace(mesh, 'CG', 1, constrained_domain=pbc)
    Stemp = FunctionSpace(mesh, 'CG', 1, constrained_domain=pbc)
    Smu = FunctionSpace(mesh, 'CG', 1, constrained_domain=pbc)
    Sgradp = VectorFunctionSpace(mesh, 'CG', 2, constrained_domain=pbc)
    Srho = FunctionSpace(mesh, 'CG', 1, constrained_domain=pbc)
    S0 = MixedFunctionSpace([Svel, Spre, Stemp])

    u = Function(S0)
    v, p, T = split(u)
    # v, p, T = u.split()

    # print(type(v), v)
    # print(type(u.sub(0)), u.sub(0))
    v_t, p_t, T_t = TestFunctions(S0)

    T0 = interpolate(TempExp(), Stemp)

    muExp = Expression('exp(-Ep * (T_val * dTemp - 1573) + cc * x[2] / meshHeight)',
                       Ep=Ep, dTemp=dTemp, cc=cc, meshHeight=MeshHeight, T_val=T0)

    mu = interpolate(muExp, Smu)

    rhosolid = Function(Srho)
    deltarho = Function(Srho)

    v0 = Function(Svel)
    vmelt = Function(Svel)

    Tf = Function(Stemp)

    v_theta = (1. - theta) * v0 + theta * v

    T_theta = (1. - theta) * T + theta * T0

    r_v = (inner(sym(grad(v_t)), 2. * mu * sym(grad(v)))
           - div(v_t) * p
           - T * v_t[1]) * dx

    r_p = p_t * div(v) * dx

    k_s = Constant(1.0E-6)

    r_T = (T_t * ((T - T0) + dt * inner(v_theta, grad(T_theta)))
           + (dt / Ra) * inner(grad(T_t), grad(T_theta))
           + T_t * k_s * (Tf - T_theta) * dt) * dx

    r = r_v + r_p + r_T

    bcv0 = DirichletBC(S0.sub(0), noslip, top)
    bcv1 = DirichletBC(S0.sub(0), vslip, bottom)
    bcp0 = DirichletBC(S0.sub(1), Constant(0.0), bottom)
    bct0 = DirichletBC(S0.sub(2), Constant(temp_values[0]), top)
    bct1 = DirichletBC(S0.sub(2), Constant(temp_values[3]), bottom)

    bcs = [bcv0, bcv1, bcp0, bct0, bct1]

    t = 0
    count = 0
    while (t < tEnd):
        Tf.interpolate(FluidTemp(T0))
        Tf.interpolate(T0)

        solve(r == 0, u, bcs)
        nV, nP, nT = u.split()

        gp = grad(nP)
        rhosolid = rho_0 * (1 - alpha * (nT * dTemp - 1573))
        deltarho = rhosolid - rhomelt
        yvec = Constant((0.0, 1.0))
        vmelt = nV * w0 - darcy * (gp * p0 / h - deltarho * yvec * g)

        if count % output_every == 0:
            fluidTemp << Tf
            pfile << nP
            ufile << nV
            tfile << nT
            mufile << mu
            gradpfile << project(grad(nP), Sgradp)
            mufile << project(mu * mu_a, Smu)
            rhofile << project(rhosolid, Srho)
            vmeltfile << project(vmelt, Svel)

        assign(T0, nT)
        assign(v0, nV)
        mu.interpolate(muExp)

        print(t)
        t += dt
        count += 1

    print('Case mu=%g, Tb=%g complete.' % (mu_a, Tb), ' Run time =', clock() - runtimeInit, 's')

if __name__ == "__main__":
    Tbs = [1300]
    Mus = [5e21]
    for mu in Mus:
        mufolder = 'mu=' + str(mu)
        try:
            makedirs(mufolder)
        except:
            pass
        for temp in Tbs:
            tempfolder = 'Tb=' + str(temp)
            workpath = mufolder + '/' + tempfolder
            RunJob(temp, mu, workpath)
