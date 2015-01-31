import numpy as np
from ..util.general import multigrid, samples_multidimensional_uniform, reshape, WKmeans
from scipy.stats import norm
import scipy
import GPyOpt
import random
from functools import reduce
import operator

def random_batch_optimization(acquisition, bounds, acqu_optimize_restarts, acqu_optimize_method, model, n_inbatch):
    '''
    Computes de batch optimization taking random samples (only for comparative purposes)
    '''
    X_batch = optimize_acquisition(acquisition, bounds, acqu_optimize_restarts, acqu_optimize_method, model)
    k=1 
    while k<n_inbatch:
        new_sample = samples_multidimensional_uniform(bounds,1)
        X_batch = np.vstack((X_batch,new_sample))  
        k +=1
    return X_batch


def adaptive_batch_optimization(acquisition, bounds, acqu_optimize_restarts, acqu_optimize_method, model, n_inbatch, alpha_L, alpha_Min):
    '''
    Computes batch optimization using by acquisition penalization using Lipschitz inference

    :param acquisition: acquisition function in which the batch selection is based
    :param bounds: the box constrains of the optimization
    :restarts: the number of restarts in the optimization of the surrogate
    :method: the method to optimize the acquisition function
    :model: the GP model based on the current samples
    :n_inbatch: the number of samples to collect
    :alpha_L: z quantile for the estimation of the Lipschitz constant L
    :alpha_Min: z quantile for the estimation of the minimum Min
    '''
    X_batch = optimize_acquisition(acquisition, bounds, acqu_optimize_restarts, acqu_optimize_method, model, X_batch=None, L=None, Min=None)
    k=1
    if n_inbatch>1:
        L = estimate_L(model,bounds,alpha_L)		          # Estimation of the Lipschitz constant
        Min = estimate_Min(model,bounds,alpha_Min)            # Estimation of the minimum value

    while k<n_inbatch:
        new_sample = optimize_acquisition(acquisition, bounds, acqu_optimize_restarts, acqu_optimize_method, model, X_batch, L, Min)
        X_batch = np.vstack((X_batch,new_sample))  
        k +=1
    return X_batch


def hybrid_batch_optimization(acqu_name, acquisition_par, acquisition, bounds, acqu_optimize_restarts, acqu_optimize_method, model, n_inbatch):   
    '''
    Computes batch optimzation using by acquisition penalization using Lipschitz inference

    :param acquisition: acquisition function in which the batch selection is based
    :param bounds: the box constrains of the optimization
    :restarts: the number of restarts in the optimization of the surrogate
    :method: the method to optimize the acquisition function
    :model: the GP model based on the current samples
    :n_inbatch: the number of samples to collect
    :alpha_L: z quantile for the estimation of the Lipschitz constant L
    :alpha_Min: z quantile for the estimation of the minimum Min
    '''
    model_copy = model.copy()
    X = model_copy.X 
    Y = model_copy.Y
    input_dim = X.shape[1] 
    kernel = model_copy.kern    
    X_new = optimize_acquisition(acquisition, bounds, acqu_optimize_restarts, acqu_optimize_method, model, X_batch=None, L=None, Min=None)
    X_batch = reshape(X_new,input_dim)
    k=1
    while k<n_inbatch:
        X = np.vstack((X,reshape(X_new,input_dim)))       # update the sample within the batch
        Y = np.vstack((Y,model.predict(reshape(X_new, input_dim))[0]))
       
        try: # this exception is included in case two equal points are selected in a batch, in this case the method stops
            batchBO = GPyOpt.methods.BayesianOptimization(f=0, 
                                        bounds= bounds, 
                                        X=X, 
                                        Y=Y, 
                                        kernel = kernel,
                                        acquisition = acqu_name, 
                                        acquisition_par = acquisition_par)
        except np.linalg.linalg.LinAlgError:
            print 'Optimization stopped. Two equal points selected.'
            break        

        batchBO.start_optimization(max_iter = 0, 
                                    n_inbatch=1, 
                                    acqu_optimize_method = acqu_optimize_method,  
                                    acqu_optimize_restarts = acqu_optimize_restarts, 
                                    stop_criteria = 1e-6,verbose = False)
        
        X_new = batchBO.suggested_sample
        X_batch = np.vstack((X_batch,X_new))
        model_batch = batchBO.model
        k+=1    
    return X_batch


def sm_batch_optimization(model, n_inbatch, batch_labels):
    n = model.X.shape[0]
    if(n<n_inbatch):
        print 'Initial points should be larger than the batch size'
    weights = np.zeros((n,1))
    X = model.X
    
    ## compute weights
    for k in np.unique(batch_labels):
        x = X[(batch_labels == k)[:,0],:]
        weights[(batch_labels == k)[:,0],:] = compute_batch_weigths(x,model)
        
        ## compute centroids
        X_batch = WKmeans(X,weights,n_inbatch)
    return np.vstack(X_batch)


def compute_w(mu,Sigma):
    n_data = Sigma.shape[0]
    w = np.zeros((n_data,1))
    Sigma12 = scipy.linalg.sqrtm(np.linalg.inv(Sigma)).real
    probabilities = norm.cdf(np.dot(Sigma12,mu))
   
    for i in range(n_data):
        w[i,:] = reduce(operator.mul, np.delete(probabilities,i,0), 1)
    return w

def compute_batch_weigths(x,model):
    Sigma = model.kern.K(x)
    mu = model.predict(x)[0]
    w = compute_w(mu,Sigma)
    return w
    

def estimate_L(model,bounds,alpha=0.025):
    '''
    Estimate the Lipschitz constant of f by taking maximizing the norm of the expectation of the gradient of  f.
    '''
    def df(x,model,alpha):
        x = reshape(x,model.X.shape[1])
        dmdx, dsdx = model.predictive_gradients(x)
        res = np.sqrt((dmdx*dmdx).sum(1)) # simply take the norm of the expectation of the gradient
        return -res
   
    samples = samples_multidimensional_uniform(bounds,5)
    pred_samples = df(samples,model,alpha)
    x0 = samples[np.argmin(pred_samples)]
    minusL = scipy.optimize.minimize(df,x0, method='SLSQP',bounds=bounds, args = (model,alpha)).fun[0][0]
    L = -minusL
    if L<0.1: L=100  ## to avoid problems in cases in which the model is flat.
    return L


def estimate_Min(model,bounds,alpha=0.025):
    '''
    Takes the estimated minumum as the minimum value in the sample
    '''
    return model.Y.min()


def hammer_function(x,x0,L,Min,model):
    '''
    Creates the function to define the esclusion zones
    '''
    x0 = x0.reshape(1,len(x0))
    m = model.predict(x0)[0]
    pred = model.predict(x0)[1].copy()
    pred[pred<1e-16] = 1e-16
    s = np.sqrt(pred)
    r_x0 = (m-Min)/L
    s_x0 = s/L
    return (norm.cdf((np.sqrt(((x-x0)**2).sum(1))- r_x0)/s_x0)).T


def penalized_acquisition(x, acquisition, bounds, model, X_batch=None, L=None, Min=None):
    '''
    Creates a penalized acquisition function using 'hammer' functions around the points collected in the batch
    '''
    sur_min = min(-acquisition(model.X))  # assumed minimum of the minus acquisition
    fval = -acquisition(x)-np.sign(sur_min)*(abs(sur_min)) 
    if X_batch!=None:
        X_batch = reshape(X_batch,model.X.shape[1]) ## 
        for i in range(X_batch.shape[0]):            
            fval = np.multiply(fval,hammer_function(x, X_batch[i,], L, Min, model))
    return -fval


def optimize_acquisition(acquisition, bounds, acqu_optimize_restarts, acqu_optimize_method, model, X_batch=None, L=None, Min=None):
    '''
    Optimization of the aquisition function
    '''
    if acqu_optimize_method=='brute':
        res = full_acquisition_optimization(acquisition,bounds,acqu_optimize_restarts, model, 'brute', X_batch, L, Min)
    elif acqu_optimize_method=='random':
        res =  full_acquisition_optimization(acquisition,bounds,acqu_optimize_restarts, model, 'random', X_batch, L, Min)
    elif acqu_optimize_method=='fast_brute':
        res =  fast_acquisition_optimization(acquisition,bounds,acqu_optimize_restarts, model, 'brute', X_batch, L, Min)
    elif acqu_optimize_method=='fast_random':
        res =  fast_acquisition_optimization(acquisition,bounds,acqu_optimize_restarts, model, 'random', X_batch, L, Min)
    return res


def fast_acquisition_optimization(acquisition, bounds,acqu_optimize_restarts, model, method_type, X_batch=None, L=None, Min=None):
    '''
    Optimizes the acquisition function using a local optimizer in the best point
    '''
    if method_type=='random':
                samples = samples_multidimensional_uniform(bounds,acqu_optimize_restarts)
    else:
        samples = multigrid(bounds, acqu_optimize_restarts)
    pred_samples = acquisition(samples)
    x0 =  samples[np.argmin(pred_samples)]
    res = scipy.optimize.minimize(penalized_acquisition, x0=np.array(x0),method='SLSQP',bounds=bounds, args=(acquisition, bounds, model, X_batch, L, Min))
    return res.x


def full_acquisition_optimization(acquisition, bounds, acqu_optimize_restarts, model, method_type, X_batch=None, L=None, Min=None):
    '''
    Optimizes the acquisition function by taking the best of a number of local optimizers
    '''
    if method_type=='random':
        samples = samples_multidimensional_uniform(bounds,acqu_optimize_restarts)
    else:
        samples = multigrid(bounds, acqu_optimize_restarts)
    mins = np.zeros((acqu_optimize_restarts,len(bounds)))
    fmins = np.zeros(acqu_optimize_restarts)
    for k in range(acqu_optimize_restarts):
        res = scipy.optimize.minimize(penalized_acquisition, x0 = samples[k,:] ,method='SLSQP',bounds=bounds, args=(acquisition, bounds, model, X_batch, L, Min))
        mins[k] = res.x
        fmins[k] = res.fun
    return mins[np.argmin(fmins)]



